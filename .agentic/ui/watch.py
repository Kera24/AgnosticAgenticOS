"""Filesystem watcher: turns changes made by the orchestration core (or a
CLI run in another terminal) into SSE events, so the dashboard stays live
without owning the orchestration loop.

Polling is deliberate: the state files are tiny, the interval is coarse, and
polling works identically on Windows/OneDrive paths where file events are
unreliable.
"""
import json
import os
import threading

from core.redact import redact

POLL_SECONDS = 1.5
MAX_LINE = 4000


class StateWatcher:
    def __init__(self, agentic_dir, bus):
        self.agentic_dir = str(agentic_dir)
        self.bus = bus
        self._stop = threading.Event()
        self._decisions_pos = None
        self._mtimes = {}
        self._thread = None

    # -- paths ---------------------------------------------------------------
    def _memory(self, name):
        return os.path.join(self.agentic_dir, "memory", name)

    def _project(self, name):
        return os.path.join(self.agentic_dir, "project", name)

    def start(self):
        # baseline the decisions offset so only NEW events stream
        path = self._memory("decisions.jsonl")
        self._decisions_pos = os.path.getsize(path) \
            if os.path.exists(path) else 0
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="agentic-ui-watch")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(POLL_SECONDS):
            try:
                self._tick()
            except Exception:   # watcher must never die
                pass

    def _tick(self):
        self._tail_decisions()
        watched = {
            "scheduler": self._memory("scheduler.json"),
            "backends": self._memory("backends.json"),
            "progress": self._project("progress.yaml"),
            "blockers": self._project("blockers.yaml"),
            "backlog": self._project("backlog.yaml"),
            "final_audit": self._project("final-audit.yaml"),
        }
        for key, path in watched.items():
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if self._mtimes.get(key) != mtime:
                first = key not in self._mtimes
                self._mtimes[key] = mtime
                if not first:
                    self.bus.publish("state", {"changed": key})

    def _tail_decisions(self):
        path = self._memory("decisions.jsonl")
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size < (self._decisions_pos or 0):
            self._decisions_pos = 0   # rotated/truncated
        if size == self._decisions_pos:
            return
        with open(path, encoding="utf-8", errors="replace") as fh:
            fh.seek(self._decisions_pos or 0)
            chunk = fh.read(512 * 1024)
            self._decisions_pos = fh.tell()
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                event = {"event": "malformed", "raw": redact(line)[:MAX_LINE]}
            self.bus.publish("activity", {"entry": event})
