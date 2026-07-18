"""Per-task worktrees, file-ownership claims, and project leases.

Layout (all under the project's machine-local runtime dir):

    <runtime>/worktrees/project            integration worktree
                                           (branch agentic/project)
    <runtime>/worktrees/tasks/<task-id>    one worktree per task
                                           (branch agentic/task/<task-id>)
    <runtime>/worktrees/ownership.json     active file-ownership claims
    <runtime>/lease.json                   project execution lease

Rules:
- a task claims its allowed paths BEFORE dispatch; overlapping claims —
  and any two claims that both touch migrations or dependency manifests —
  are refused;
- checks/review run inside the task worktree; only passing work is
  committed there and merged locally into agentic/project;
- the integration target must be clean; a conflicted merge is aborted and
  the task worktree kept as evidence;
- successful worktrees are removed; failed ones are preserved per
  retention policy; abandoned ones are recoverable after restart;
- nothing here ever pushes, touches a remote, or edits the user's own
  branches;
- a lease (machine + pid + expiry) prevents two processes/machines from
  running the same project concurrently.
"""
import datetime as _dt
import json
import os

from . import errors, gitops

TASK_BRANCH_PREFIX = "agentic/task/"
OWNERSHIP_FILE = "ownership.json"
LEASE_FILE = "lease.json"
DEFAULT_LEASE_SECONDS = 3600
ABANDONED_AFTER_SECONDS = 24 * 3600

# any two tasks both touching one of these classes conflict outright
EXCLUSIVE_CLASSES = {
    "migrations": ("**/migrations/**", "supabase/migrations/**", "**/*.sql"),
    "dependencies": ("package.json", "package-lock.json", "requirements*.txt",
                     "pyproject.toml", "go.mod", "Cargo.toml", "Gemfile",
                     "pnpm-lock.yaml", "yarn.lock"),
    "docker": ("docker-compose.yml", "compose.yaml", "compose.yml",
               "Dockerfile"),
}


def _now():
    return _dt.datetime.now()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


# -- ownership claims ------------------------------------------------------------

def _ownership_path(agentic_dir):
    return os.path.join(agentic_dir, "worktrees", OWNERSHIP_FILE)


def _load_ownership(agentic_dir):
    path = _ownership_path(agentic_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return {}


def _save_ownership(agentic_dir, claims):
    path = _ownership_path(agentic_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(claims, fh, indent=2)
    os.replace(tmp, path)


def _classes_touched(patterns):
    touched = set()
    for cls, class_patterns in EXCLUSIVE_CLASSES.items():
        for pattern in patterns:
            if gitops.matches_any(pattern, class_patterns) or any(
                    gitops.match_pattern(pattern, cp)
                    for cp in class_patterns):
                touched.add(cls)
    return touched


def _patterns_overlap(a_patterns, b_patterns):
    for a in a_patterns:
        for b in b_patterns:
            if a == b or gitops.match_pattern(a, b) or \
                    gitops.match_pattern(b, a):
                return True
    return False


def claim_paths(agentic_dir, task_id, patterns, run_id=None):
    """Record expected file ownership; refuse overlaps. Returns the claim
    or raises PolicyError naming the conflicting task."""
    patterns = sorted(set(patterns or []))
    claims = _load_ownership(agentic_dir)
    mine_classes = _classes_touched(patterns)
    for other_id, other in claims.items():
        if other_id == task_id:
            continue
        if _patterns_overlap(patterns, other.get("paths", [])):
            raise errors.PolicyError(
                "file ownership overlap: task %s already claims paths "
                "intersecting %s" % (other_id, patterns))
        shared = mine_classes & _classes_touched(other.get("paths", []))
        if shared:
            raise errors.PolicyError(
                "exclusive-resource conflict with task %s (%s): only one "
                "task may touch %s at a time"
                % (other_id, ", ".join(sorted(shared)),
                   ", ".join(sorted(shared))))
    claims[task_id] = {"paths": patterns, "run_id": run_id,
                       "claimed_at": _iso(_now())}
    _save_ownership(agentic_dir, claims)
    return claims[task_id]


def release_claim(agentic_dir, task_id):
    claims = _load_ownership(agentic_dir)
    if task_id in claims:
        del claims[task_id]
        _save_ownership(agentic_dir, claims)


def active_claims(agentic_dir):
    return _load_ownership(agentic_dir)


# -- per-task worktrees -----------------------------------------------------------

def task_worktree_path(agentic_dir, task_id):
    return os.path.join(agentic_dir, "worktrees", "tasks", task_id)


def create_task_worktree(root, agentic_dir, task_id, base_branch):
    """Worktree on branch agentic/task/<task-id>, based on the current tip
    of the project's agentic branch (work accumulates)."""
    path = task_worktree_path(agentic_dir, task_id)
    branch = TASK_BRANCH_PREFIX + task_id
    if os.path.exists(os.path.join(path, ".git")):
        return path                        # resuming an interrupted task
    os.makedirs(os.path.dirname(path), exist_ok=True)
    base = base_branch if _branch_exists(root, base_branch) else "HEAD"
    existing = gitops.run_git(["branch", "--list", branch], cwd=root,
                              check=False)
    if branch.split("/")[-1] in existing or branch in existing:
        gitops.run_git(["worktree", "add", path, branch], cwd=root)
    else:
        gitops.run_git(["worktree", "add", "-b", branch, path, base],
                       cwd=root)
    return path


def _branch_exists(root, branch):
    return bool(gitops.run_git(["branch", "--list", branch], cwd=root,
                               check=False).strip())


def integrate_task(root, project_worktree, task_worktree, task_id,
                   message):
    """Locally merge the task branch into agentic/project. The target must
    be clean; conflicts abort and preserve the task worktree as evidence.
    Never touches a remote or the user's own branches."""
    branch = TASK_BRANCH_PREFIX + task_id
    dirty = gitops.run_git(["status", "--porcelain"], cwd=project_worktree,
                           check=False).strip()
    if dirty:
        raise errors.PolicyError(
            "integration target (agentic/project) is dirty; refusing to "
            "merge task %s" % task_id)
    merge = gitops.run_git(["merge", "--no-ff", "-m", message, branch],
                           cwd=project_worktree, check=False)
    conflicted = gitops.run_git(["status", "--porcelain"],
                                cwd=project_worktree,
                                check=False)
    if any(line.startswith(("UU", "AA", "DD")) or "U" in line[:2]
           for line in conflicted.splitlines()):
        gitops.run_git(["merge", "--abort"], cwd=project_worktree,
                       check=False)
        raise errors.PolicyError(
            "merge conflict integrating task %s; task worktree preserved "
            "as evidence" % task_id)
    return merge


def cleanup_task_worktree(root, agentic_dir, task_id, success,
                          keep_failed=True):
    """Remove a successful task worktree + branch; preserve failed ones
    (retention policy) so the evidence survives."""
    path = task_worktree_path(agentic_dir, task_id)
    branch = TASK_BRANCH_PREFIX + task_id
    if not success and keep_failed:
        return {"kept": path}
    gitops.run_git(["worktree", "remove", "--force", path], cwd=root,
                   check=False)
    gitops.run_git(["branch", "-D", branch], cwd=root, check=False)
    release_claim(agentic_dir, task_id)
    return {"removed": path}


def recover_abandoned(root, agentic_dir, clock=None):
    """After a crash/restart: prune git's stale worktree records and report
    task worktrees without an active claim (older than the abandonment
    window) so they can be resumed or cleaned deliberately."""
    clock = clock or _now
    gitops.run_git(["worktree", "prune"], cwd=root, check=False)
    tasks_dir = os.path.join(agentic_dir, "worktrees", "tasks")
    claims = _load_ownership(agentic_dir)
    abandoned = []
    if os.path.isdir(tasks_dir):
        for task_id in os.listdir(tasks_dir):
            path = os.path.join(tasks_dir, task_id)
            if task_id in claims or not os.path.isdir(path):
                continue
            try:
                age = clock().timestamp() - os.path.getmtime(path)
            except OSError:
                continue
            if age > ABANDONED_AFTER_SECONDS:
                abandoned.append({"task_id": task_id, "path": path,
                                  "age_seconds": int(age)})
    return abandoned


# -- project leases ----------------------------------------------------------------

def machine_id():
    return (os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME")
            or "local")


class ProjectLease:
    """Machine+process execution lease with expiry. Prevents the same
    project from running in two processes (or machines sharing state)."""

    def __init__(self, agentic_dir, project_id, ttl_seconds=None,
                 clock=None):
        self.path = os.path.join(agentic_dir, LEASE_FILE)
        self.project_id = project_id
        self.ttl = int(ttl_seconds or DEFAULT_LEASE_SECONDS)
        self.clock = clock or _now

    def _read(self):
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            return None

    def _write(self, lease):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(lease, fh, indent=2)
        os.replace(tmp, self.path)

    def holder(self):
        """The current unexpired lease, or None."""
        lease = self._read()
        if not lease or lease.get("status") != "active":
            return None
        try:
            expires = _dt.datetime.fromisoformat(lease["expires_at"])
        except (KeyError, ValueError):
            return None
        if self.clock() >= expires:
            return None
        return lease

    def _is_mine(self, lease):
        return lease.get("machine_id") == machine_id() and \
            lease.get("pid") == os.getpid()

    def acquire(self, run_id=None):
        """(acquired: bool, lease_or_holder). Renews when already ours."""
        holder = self.holder()
        if holder and not self._is_mine(holder):
            return False, holder
        now = self.clock()
        lease = {
            "project_id": self.project_id,
            "machine_id": machine_id(),
            "pid": os.getpid(),
            "run_id": run_id,
            "acquired_at": (holder or {}).get("acquired_at") or _iso(now),
            "renewed_at": _iso(now),
            "expires_at": _iso(now + _dt.timedelta(seconds=self.ttl)),
            "status": "active",
        }
        self._write(lease)
        return True, lease

    def renew(self):
        holder = self.holder()
        if holder and self._is_mine(holder):
            return self.acquire(run_id=holder.get("run_id"))[0]
        return False

    def release(self):
        holder = self.holder()
        if holder and not self._is_mine(holder):
            return False
        if holder:
            holder["status"] = "released"
            self._write(holder)
        return True
