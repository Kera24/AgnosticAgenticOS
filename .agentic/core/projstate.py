"""Persistent project state under .agentic/project/.

Files: PROJECT.md, architecture.md, acceptance-criteria.yaml,
milestones.yaml, backlog.yaml, decisions.yaml, blockers.yaml, progress.yaml,
final-audit.yaml.

All YAML writes are atomic (tmp + os.replace); mutations happen under a lock
file so an interrupted or concurrent process never corrupts state. The
structure fully survives process/computer restarts — nothing completed is
ever regenerated.
"""
import datetime as _dt
import os

import yaml

from . import errors

TASK_DEFAULTS = {
    "id": None, "milestone": None, "description": "",
    "dependencies": [], "risk": "medium", "security_relevant": False,
    "expected_paths": [], "expected_size": "medium",
    "acceptance_criteria": [], "deterministic_checks": [],
    "status": "pending",          # pending|in_progress|done|blocked|abandoned
    "attempts": 0, "replans": 0, "last_result": None, "blocking_reason": None,
    "skill": None,
    # "bootstrap" (scaffolding, no test framework yet), "test_setup" (the
    # task that introduces the test framework), or unset for ordinary
    # feature/business-logic work -- see core.bootstrap_gate.
    "kind": None,
}

FILES = ["PROJECT.md", "architecture.md", "acceptance-criteria.yaml",
         "milestones.yaml", "backlog.yaml", "decisions.yaml",
         "blockers.yaml", "progress.yaml"]


def project_dir(agentic_dir):
    return os.path.join(str(agentic_dir), "project")


def exists(agentic_dir):
    return os.path.exists(os.path.join(project_dir(agentic_dir),
                                       "backlog.yaml"))


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def write_yaml(agentic_dir, name, data):
    _atomic_write(os.path.join(project_dir(agentic_dir), name),
                  yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def read_yaml(agentic_dir, name, default=None):
    path = os.path.join(project_dir(agentic_dir), name)
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or default


def write_text(agentic_dir, name, text):
    _atomic_write(os.path.join(project_dir(agentic_dir), name), text)


class ProjectLock:
    """Cross-process lock (O_CREAT|O_EXCL). Stale locks (dead beyond
    `stale_seconds`) are broken so a crashed cycle cannot deadlock resume."""

    def __init__(self, agentic_dir, name="project.lock", stale_seconds=7200):
        self.path = os.path.join(project_dir(agentic_dir), name)
        self.stale_seconds = stale_seconds
        self.fd = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, str(os.getpid()).encode())
            return True
        except FileExistsError:
            try:
                age = _dt.datetime.now().timestamp() - os.path.getmtime(self.path)
            except OSError:
                return False
            if age > self.stale_seconds:
                try:
                    os.remove(self.path)
                except OSError:
                    return False
                return self.acquire()
            return False

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            os.remove(self.path)
        except OSError:
            pass

    def __enter__(self):
        if not self.acquire():
            raise errors.PolicyError("another cycle is already running "
                                     "(lock: %s)" % self.path)
        return self

    def __exit__(self, *exc):
        self.release()


# -- backlog operations --------------------------------------------------------

def normalize_task(task):
    merged = dict(TASK_DEFAULTS)
    merged.update(task or {})
    if not merged["id"]:
        raise errors.PolicyError("backlog task missing stable id")
    return merged


def load_backlog(agentic_dir):
    data = read_yaml(agentic_dir, "backlog.yaml", {"tasks": []})
    return [normalize_task(t) for t in data.get("tasks", [])]


def save_backlog(agentic_dir, tasks):
    write_yaml(agentic_dir, "backlog.yaml", {"tasks": tasks})


def milestone_order(agentic_dir):
    data = read_yaml(agentic_dir, "milestones.yaml", {"milestones": []})
    return [m["id"] for m in data.get("milestones", [])]


def next_task(agentic_dir):
    """Dependency-aware selection: first pending task, in milestone order,
    whose dependencies are all done. Never re-selects completed work."""
    tasks = load_backlog(agentic_dir)
    done = {t["id"] for t in tasks if t["status"] == "done"}
    order = milestone_order(agentic_dir)
    rank = {m: i for i, m in enumerate(order)}

    def key(task):
        return (rank.get(task["milestone"], len(order)),
                task["id"])

    for task in sorted(tasks, key=key):
        if task["status"] != "pending":
            continue
        if any(dep not in done for dep in task.get("dependencies", [])):
            continue
        return task
    return None


def update_task(agentic_dir, task_id, **fields):
    tasks = load_backlog(agentic_dir)
    for task in tasks:
        if task["id"] == task_id:
            task.update(fields)
            break
    else:
        raise errors.PolicyError("unknown task id %r" % task_id)
    save_backlog(agentic_dir, tasks)
    refresh_progress(agentic_dir)
    return task


def refresh_progress(agentic_dir):
    tasks = load_backlog(agentic_dir)
    by_status = {}
    for task in tasks:
        by_status[task["status"]] = by_status.get(task["status"], 0) + 1
    milestones = read_yaml(agentic_dir, "milestones.yaml",
                           {"milestones": []}).get("milestones", [])
    milestone_state = {}
    for milestone in milestones:
        mid = milestone["id"]
        mtasks = [t for t in tasks if t["milestone"] == mid]
        milestone_state[mid] = (
            "done" if mtasks and all(t["status"] == "done" for t in mtasks)
            else "blocked" if any(t["status"] == "blocked" for t in mtasks)
            else "in_progress" if any(t["status"] != "pending" for t in mtasks)
            else "pending")
    progress = {"updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
                "tasks_total": len(tasks), "tasks_by_status": by_status,
                "milestones": milestone_state,
                "backlog_complete": bool(tasks) and
                all(t["status"] in ("done", "abandoned") for t in tasks)}
    write_yaml(agentic_dir, "progress.yaml", progress)
    return progress


# Stable blocker classification (never free-form reason text): the code a
# blocker was recorded with is what dedup and recovery key off. `code` is
# optional and defaults to None for call sites that predate this taxonomy
# or that don't fit it -- those keep the old (no-dedup) behaviour exactly.
BLOCKER_CODE_DETERMINISTIC_CHECKS_MISSING = "deterministic_checks_missing"
BLOCKER_CODE_GENUINE_HUMAN_DECISION = "genuine_human_decision"
BLOCKER_CODE_POLICY_DENIED = "policy_denied"
BLOCKER_CODE_DEPENDENCY_MISSING = "dependency_missing"
BLOCKER_CODE_AUTHENTICATION_REQUIRED = "authentication_required"
BLOCKER_CODES = (BLOCKER_CODE_DETERMINISTIC_CHECKS_MISSING,
                 BLOCKER_CODE_GENUINE_HUMAN_DECISION,
                 BLOCKER_CODE_POLICY_DENIED,
                 BLOCKER_CODE_DEPENDENCY_MISSING,
                 BLOCKER_CODE_AUTHENTICATION_REQUIRED)


def add_blocker(agentic_dir, task_id, reason, human_only=False, code=None):
    """Append a blocker unless an unresolved one already exists for the
    same (task_id, code) -- a stable code, never free-form reason text,
    is what prevents duplicate human/non-human records for one failure."""
    blockers = read_yaml(agentic_dir, "blockers.yaml", {"blockers": []})
    if code is not None and any(
            not b.get("resolved") and b.get("task") == task_id and
            b.get("code") == code for b in blockers["blockers"]):
        return
    blockers["blockers"].append({
        "task": task_id, "reason": reason, "code": code,
        "human_only": bool(human_only),
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "resolved": False})
    write_yaml(agentic_dir, "blockers.yaml", blockers)


def open_blockers(agentic_dir, human_only=None):
    blockers = read_yaml(agentic_dir, "blockers.yaml",
                         {"blockers": []}).get("blockers", [])
    out = [b for b in blockers if not b.get("resolved")]
    if human_only is not None:
        out = [b for b in out if bool(b.get("human_only")) == human_only]
    return out


def milestone_of(agentic_dir, task_id):
    for task in load_backlog(agentic_dir):
        if task["id"] == task_id:
            return task["milestone"]
    return None
