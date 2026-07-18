"""Multi-project fleet scheduler.

Runs up to N registered projects through the EXISTING single-project cycle
engine without ever exceeding the configured resource pools. Decisions are
pure and persisted; execution is a thin threaded layer around an injectable
per-project runner (tests never run real cycles).

State (machine-local runtime home):
    fleet.json               global pause flag
    slots.json               live slot allocations (reaped when stale/dead)
    fleet-decisions.jsonl    every scheduling decision with reasons

Slot pools (config `concurrency:`): active projects, model calls (global,
per backend, per project), docker builds, heavy local models, test jobs,
per-project database mutations. Capacity remains labelled
reported/estimated/unknown by the existing capacity layer.
"""
import datetime as _dt
import json
import os
import threading

from . import projstate
from .redact import redact

DEFAULT_CONCURRENCY = {
    "maximum_registered_projects": 100,
    "maximum_active_projects": 4,
    "maximum_model_calls": 2,
    "maximum_docker_builds": 1,
    "maximum_heavy_local_models": 1,
    "maximum_test_jobs": 2,
    "per_backend": {"claude": 1, "codex": 1, "qwen": 1, "ollama": 1},
    "per_project": {"maximum_model_calls": 1,
                    "maximum_database_mutations": 1},
}

SLOT_TTL_SECONDS = 2 * 3600

PROJECT_STATES = (
    "uninitialised", "ready", "queued", "preparing", "running", "testing",
    "reviewing", "repairing", "cooling", "paused", "blocked", "completed",
    "failed", "archived")


def _now():
    return _dt.datetime.now()


def concurrency_config(cfg):
    merged = json.loads(json.dumps(DEFAULT_CONCURRENCY))
    raw = cfg.get("concurrency") or {}
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


# -- slot accounting --------------------------------------------------------------

class SlotManager:
    """Persisted slot allocations. Stale entries (expired, or dead pid on
    this machine) are reaped on load, so a crash always releases its
    resources on the next tick."""

    def __init__(self, home, clock=None):
        self.path = os.path.join(home, "slots.json")
        self.clock = clock or _now
        self._lock = threading.Lock()

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                allocations = json.load(fh)
        except (ValueError, OSError):
            return {}
        return self._reap(allocations)

    def _reap(self, allocations):
        alive = {}
        now = self.clock()
        for project_id, entry in allocations.items():
            try:
                expires = _dt.datetime.fromisoformat(entry["expires_at"])
            except (KeyError, ValueError):
                continue
            if now >= expires:
                continue
            pid = entry.get("pid")
            if pid and entry.get("machine") == _machine() and \
                    not _pid_alive(pid):
                continue
            alive[project_id] = entry
        return alive

    def _save(self, allocations):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(allocations, fh, indent=2)
        os.replace(tmp, self.path)

    def usage(self):
        allocations = self._load()
        totals = {"active_projects": len(allocations), "model": 0,
                  "docker_build": 0, "heavy_local": 0, "test_job": 0,
                  "per_backend": {}}
        for entry in allocations.values():
            for slot, count in (entry.get("slots") or {}).items():
                if slot.startswith("backend:"):
                    name = slot.split(":", 1)[1]
                    totals["per_backend"][name] = \
                        totals["per_backend"].get(name, 0) + count
                else:
                    totals[slot] = totals.get(slot, 0) + count
        return dict(totals, allocations=allocations)

    def acquire(self, cfg, project_id, backend, extra_slots=None):
        """Atomically claim the slots one project cycle needs. Returns
        (ok, reason)."""
        limits = concurrency_config(cfg)
        wanted = {"model": 1}
        wanted.update(extra_slots or {})
        if backend:
            wanted["backend:%s" % backend] = 1
        with self._lock:
            allocations = self._load()
            if project_id in allocations:
                return False, "project already holds an execution slot " \
                              "(no double runs)"
            if len(allocations) >= int(limits["maximum_active_projects"]):
                return False, "waiting for an active-project slot (%d in " \
                              "use)" % len(allocations)
            usage = {"model": 0, "docker_build": 0, "heavy_local": 0,
                     "test_job": 0}
            backend_usage = {}
            for entry in allocations.values():
                for slot, count in (entry.get("slots") or {}).items():
                    if slot.startswith("backend:"):
                        name = slot.split(":", 1)[1]
                        backend_usage[name] = backend_usage.get(name, 0) \
                            + count
                    else:
                        usage[slot] = usage.get(slot, 0) + count
            pools = {"model": int(limits["maximum_model_calls"]),
                     "docker_build": int(limits["maximum_docker_builds"]),
                     "heavy_local": int(
                         limits["maximum_heavy_local_models"]),
                     "test_job": int(limits["maximum_test_jobs"])}
            for slot, need in wanted.items():
                if slot.startswith("backend:"):
                    name = slot.split(":", 1)[1]
                    cap = int((limits.get("per_backend") or {})
                              .get(name, 999))
                    if backend_usage.get(name, 0) + need > cap:
                        return False, "waiting for a %s slot" % name
                elif slot in pools:
                    if usage.get(slot, 0) + need > pools[slot]:
                        return False, "waiting for a %s slot" \
                            % slot.replace("_", " ")
            allocations[project_id] = {
                "slots": wanted, "backend": backend,
                "pid": os.getpid(), "machine": _machine(),
                "acquired_at": self.clock().isoformat(timespec="seconds"),
                "expires_at": (self.clock() + _dt.timedelta(
                    seconds=SLOT_TTL_SECONDS)).isoformat(
                        timespec="seconds")}
            self._save(allocations)
            return True, "acquired"

    def release(self, project_id):
        with self._lock:
            allocations = self._load()
            allocations.pop(project_id, None)
            self._save(allocations)


def _machine():
    return os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") \
        or "local"


def _pid_alive(pid):
    """Liveness probe. NEVER uses os.kill on Windows (where any signal
    value terminates the target process)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# -- global pause ------------------------------------------------------------------

def fleet_state_path(home):
    return os.path.join(home, "fleet.json")


def load_fleet_state(home):
    path = fleet_state_path(home)
    if not os.path.exists(path):
        return {"global_pause": False, "updated_at": None}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return {"global_pause": False, "updated_at": None}


def set_global_pause(home, paused, clock=None):
    state = load_fleet_state(home)
    state["global_pause"] = bool(paused)
    state["updated_at"] = (clock or _now)().isoformat(timespec="seconds")
    os.makedirs(home, exist_ok=True)
    tmp = fleet_state_path(home) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, fleet_state_path(home))
    return state


# -- project state classification ---------------------------------------------------

def classify_project(cfg, registry, record, clock=None):
    """One of PROJECT_STATES plus a human waiting reason."""
    from . import projectops, taskspace
    from .scheduler import Scheduler
    if record["status"] == "archived":
        return "archived", "archived"
    if record["status"] == "registered":
        return "uninitialised", "run `project init` first"
    proj_cfg = projectops.project_cfg_for(cfg, registry, record)
    state_dir = proj_cfg["runtime"]["project_dir"]
    memdir = os.path.join(state_dir, "memory")
    scheduler = Scheduler(proj_cfg, memdir, clock=clock)
    sched_state = scheduler.state
    lease = taskspace.ProjectLease(state_dir, record["id"], clock=clock)
    holder = lease.holder()
    if sched_state["state"] == "complete":
        return "completed", "project complete"
    if sched_state["state"] == "paused":
        return "paused", "paused by user"
    if holder and (holder.get("pid") != os.getpid()
                   or holder.get("machine_id") != taskspace.machine_id()):
        return "running", "project lease held by %s (pid %s)" \
            % (holder.get("machine_id"), holder.get("pid"))
    if sched_state["state"] == "running":
        return "running", "cycle %s in progress" \
            % sched_state.get("current_cycle")
    if sched_state["state"] == "cooling":
        eligible, reason = scheduler.eligible()
        if not eligible:
            return "cooling", reason
    if projstate.exists(state_dir):
        blockers = projstate.open_blockers(state_dir, human_only=True)
        if blockers:
            return "blocked", "waiting for external approval: %s" \
                % blockers[0]["reason"][:120]
        if sched_state.get("project_status") == "audit_failed":
            return "failed", "final audit failed"
    if not record.get("enabled"):
        return "ready", "initialised; not started"
    if not projstate.exists(state_dir):
        return "ready", "no plan architected yet (run `project start`)"
    return "queued", "eligible"


# -- planning -----------------------------------------------------------------------

def plan(cfg, registry, clock=None, home=None):
    """Pure scheduling decision for one tick: which projects to start now,
    and exactly why every other project is waiting. Persisted to the
    decision log."""
    clock = clock or _now
    home = home or registry.home
    fleet_state = load_fleet_state(home)
    slots = SlotManager(home, clock=clock)
    limits = concurrency_config(cfg)
    records = [r for r in registry.list(include_archived=False)]
    decisions = {"at": clock().isoformat(timespec="seconds"),
                 "global_pause": fleet_state["global_pause"],
                 "start": [], "waiting": [], "states": {}}

    if fleet_state["global_pause"]:
        for record in records:
            decisions["states"][record["id"]] = "paused"
            decisions["waiting"].append(
                {"project": record["id"], "reason": "global pause active"})
        _log_decision(home, decisions)
        return decisions

    # fairness: priority first, then least-recently-scheduled
    def fairness_key(record):
        last = (record.get("metadata") or {}).get("last_scheduled_at") or ""
        return (-int(record.get("priority") or 0), last, record["id"])

    candidates = sorted(records, key=fairness_key)
    started = 0
    for record in candidates:
        project_id = record["id"]
        state, reason = classify_project(cfg, registry, record, clock=clock)
        decisions["states"][project_id] = state
        if state != "queued":
            if state in ("cooling", "blocked", "paused", "running"):
                decisions["waiting"].append({"project": project_id,
                                             "reason": reason})
            continue
        if started >= int(limits["maximum_active_projects"]):
            decisions["waiting"].append(
                {"project": project_id,
                 "reason": "waiting for an active-project slot"})
            continue
        # backend + capacity through the existing machinery
        from . import backends as backends_mod, capacity as capacity_mod, \
            projectops
        from .breaker import BreakerBoard
        proj_cfg = projectops.project_cfg_for(cfg, registry, record)
        state_dir = proj_cfg["runtime"]["project_dir"]
        memdir = os.path.join(state_dir, "memory")
        board = BreakerBoard(memdir, clock=clock)
        ledger = capacity_mod.CapacityLedger(proj_cfg, memdir, clock=clock)
        try:
            chain = backends_mod.routing_chain(proj_cfg, "coder",
                                               board=board, ledger=ledger)
        except Exception as exc:   # noqa: BLE001
            decisions["waiting"].append(
                {"project": project_id,
                 "reason": "no usable backend: %s" % str(exc)[:120]})
            continue
        task = projstate.next_task(state_dir)
        capacity = capacity_mod.decide_start(proj_cfg, task or {}, ledger,
                                             board, chain)
        if capacity["decision"] not in ("start", "reroute"):
            decisions["waiting"].append(
                {"project": project_id,
                 "reason": "insufficient estimated capacity: %s"
                           % capacity["reason"][:120],
                 "confidence": capacity["confidence"]})
            continue
        backend = capacity["selected_backend"]
        extra = {}
        if (proj_cfg.get("backends") or {}).get(backend, {}) \
                .get("type") == "local":
            extra["heavy_local"] = 1
        ok, slot_reason = slots.acquire(cfg, project_id, backend,
                                        extra_slots=extra)
        if not ok:
            decisions["waiting"].append({"project": project_id,
                                         "reason": slot_reason})
            continue
        decisions["start"].append({"project": project_id,
                                   "backend": backend,
                                   "capacity_confidence":
                                       capacity["confidence"]})
        started += 1
        registry.update(project_id, metadata=dict(
            record.get("metadata") or {},
            last_scheduled_at=clock().isoformat(timespec="seconds")))
    _log_decision(home, decisions)
    return decisions


def _log_decision(home, decisions):
    try:
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "fleet-decisions.jsonl"), "a",
                  encoding="utf-8") as fh:
            fh.write(redact(json.dumps(decisions, default=str)) + "\n")
    except OSError:
        pass


def read_decisions(home, limit=20):
    path = os.path.join(home, "fleet-decisions.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out[-limit:]


# -- execution ---------------------------------------------------------------------

def default_runner(cfg, registry, project_id):
    """Run ONE cycle for one project through the existing engine."""
    from . import projectops
    from .project import run_cycle
    record = registry.get(project_id)
    proj_cfg = projectops.project_cfg_for(cfg, registry, record)
    return run_cycle(proj_cfg)


def run_tick(cfg, registry, runner=None, clock=None, home=None):
    """One fleet tick: plan, then execute the starts concurrently. Slots
    are ALWAYS released afterwards — including on failure."""
    runner = runner or default_runner
    home = home or registry.home
    decisions = plan(cfg, registry, clock=clock, home=home)
    slots = SlotManager(home, clock=clock or _now)
    results = {}
    if not decisions["start"]:
        return dict(decisions, results=results)
    import concurrent.futures as _fut
    with _fut.ThreadPoolExecutor(
            max_workers=max(1, len(decisions["start"]))) as pool:
        futures = {}
        for entry in decisions["start"]:
            project_id = entry["project"]
            futures[pool.submit(_run_one, cfg, registry, runner,
                                project_id)] = project_id
        for future in _fut.as_completed(futures):
            project_id = futures[future]
            try:
                results[project_id] = future.result()
            except Exception as exc:   # noqa: BLE001
                results[project_id] = {"status": "failure",
                                       "detail": str(exc)[:300]}
            finally:
                slots.release(project_id)
    return dict(decisions, results=results)


def _run_one(cfg, registry, runner, project_id):
    return runner(cfg, registry, project_id)
