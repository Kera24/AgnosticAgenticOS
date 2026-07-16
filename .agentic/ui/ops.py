"""Operation manager for long-running dashboard actions.

Every mutating action (start project, run cycle, final audit, smoke test)
runs as a tracked operation in a worker thread: the HTTP request returns an
operation id immediately, progress streams over SSE, and duplicate
submissions in the same group are rejected while one is in flight. The core
ProjectLock still guards against cross-process overlap.

Cycles are never killed mid-flight (a thread cannot be safely terminated and
a half-applied cycle would corrupt nothing thanks to the worktree, but would
waste backend allowance); the safe cancellation path is `project/pause`,
which prevents the *next* cycle.
"""
import datetime as _dt
import json
import os
import threading
import traceback
import uuid


class OperationConflict(Exception):
    def __init__(self, existing):
        super().__init__("operation already running")
        self.existing = existing


class OperationManager:
    def __init__(self, bus, persist_path=None):
        self.bus = bus
        self.persist_path = persist_path
        self._lock = threading.Lock()
        self._ops = {}
        self._active_by_group = {}
        self._load_persisted()

    # -- persistence (so a reconnecting browser sees the last outcome) ------
    def _load_persisted(self):
        if self.persist_path and os.path.exists(self.persist_path):
            try:
                with open(self.persist_path, encoding="utf-8") as fh:
                    for op in json.load(fh):
                        if op.get("status") == "running":
                            op["status"] = "interrupted"
                        self._ops[op["id"]] = op
            except (ValueError, OSError):
                pass

    def _persist(self):
        if not self.persist_path:
            return
        recent = sorted(self._ops.values(),
                        key=lambda o: o["started_at"])[-50:]
        tmp = self.persist_path + ".tmp"
        os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(recent, fh, indent=1, default=str)
        os.replace(tmp, self.persist_path)

    # -- lifecycle -----------------------------------------------------------
    def start(self, kind, group, runner, detail=None):
        with self._lock:
            active_id = self._active_by_group.get(group)
            if active_id and self._ops.get(active_id, {}).get(
                    "status") == "running":
                raise OperationConflict(self._ops[active_id])
            op = {"id": uuid.uuid4().hex[:12], "kind": kind, "group": group,
                  "status": "running", "detail": detail or "",
                  "started_at": _dt.datetime.now().isoformat(
                      timespec="seconds"),
                  "finished_at": None, "result": None, "error": None}
            self._ops[op["id"]] = op
            self._active_by_group[group] = op["id"]
        self.bus.publish("operation", dict(op))
        thread = threading.Thread(target=self._run, args=(op["id"], runner),
                                  daemon=True, name="agentic-ui-op")
        thread.start()
        return dict(op)

    def _run(self, op_id, runner):
        op = self._ops[op_id]
        try:
            result = runner()
            op["status"] = "succeeded"
            op["result"] = result
        except Exception as exc:   # deliberate: any failure is reported, not raised
            op["status"] = "failed"
            op["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:400])
            op["trace"] = traceback.format_exc()[-1500:]
        op["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        with self._lock:
            if self._active_by_group.get(op["group"]) == op_id:
                del self._active_by_group[op["group"]]
            self._persist()
        public = {k: v for k, v in op.items() if k != "trace"}
        self.bus.publish("operation", public)

    # -- queries ---------------------------------------------------------------
    def get(self, op_id):
        op = self._ops.get(op_id)
        return {k: v for k, v in op.items() if k != "trace"} if op else None

    def list(self, limit=30):
        ops = sorted(self._ops.values(), key=lambda o: o["started_at"],
                     reverse=True)[:limit]
        return [{k: v for k, v in o.items() if k != "trace"} for o in ops]

    def running(self, group=None):
        with self._lock:
            ids = ([self._active_by_group.get(group)] if group
                   else list(self._active_by_group.values()))
        return [self._ops[i] for i in ids
                if i and self._ops.get(i, {}).get("status") == "running"]
