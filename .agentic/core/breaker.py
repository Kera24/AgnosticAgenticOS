"""Backend circuit breakers. States and transitions are code-driven from
observed results; no model can flip a breaker.

States: available | degraded | cooling | rate_limited | usage_exhausted |
authentication_required | unavailable

Persisted in .agentic/memory/backends.json. Recovery estimates grow while a
backend keeps failing and are informed by observed historical recoveries.
"""
import datetime as _dt
import json
import os

STATES = ["available", "degraded", "cooling", "rate_limited",
          "usage_exhausted", "authentication_required", "unavailable"]

DEFAULT_RECOVERY = {"rate_limit": 15 * 60, "usage_limit": 60 * 60,
                    "backend_unavailable": 10 * 60, "unknown": 10 * 60}
MAX_RECOVERY = 6 * 3600


def _now():
    return _dt.datetime.now()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


class BreakerBoard:
    def __init__(self, memory_dir, clock=None):
        self.path = os.path.join(memory_dir, "backends.json")
        self.clock = clock or _now
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as fh:
                    return json.load(fh)
            except ValueError:
                pass
        return {}

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)
        os.replace(tmp, self.path)

    def entry(self, backend):
        return self.data.setdefault(backend, {
            "state": "available", "unavailable_until": None,
            "consecutive_failures": 0, "recovery_history_seconds": [],
            "last_ok": None, "last_failure_kind": None, "failed_since": None})

    # -- queries -------------------------------------------------------------
    def state(self, backend):
        entry = self.entry(backend)
        self._maybe_reopen(backend, entry)
        return entry["state"]

    def is_available(self, backend):
        return self.state(backend) in ("available", "degraded")

    def unavailable_until(self, backend):
        return self.entry(backend).get("unavailable_until")

    def _maybe_reopen(self, backend, entry):
        """After the waiting period, move to `cooling` (half-open): the next
        caller must run a lightweight health check before real work."""
        until = entry.get("unavailable_until")
        if entry["state"] in ("rate_limited", "usage_exhausted",
                              "unavailable") and until:
            if _now_from(self.clock) >= _dt.datetime.fromisoformat(until):
                entry["state"] = "cooling"
                self.save()

    # -- transitions -----------------------------------------------------------
    def record_success(self, backend):
        entry = self.entry(backend)
        if entry.get("failed_since"):
            downtime = (_now_from(self.clock)
                        - _dt.datetime.fromisoformat(entry["failed_since"]))
            entry["recovery_history_seconds"] = (
                entry.get("recovery_history_seconds", [])
                + [int(downtime.total_seconds())])[-10:]
            entry["failed_since"] = None
        entry.update(state="available", unavailable_until=None,
                     consecutive_failures=0,
                     last_ok=_iso(_now_from(self.clock)))
        self.save()

    def record_failure(self, backend, kind, retry_after_seconds=None,
                       reset_at=None):
        entry = self.entry(backend)
        entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
        entry["last_failure_kind"] = kind
        if not entry.get("failed_since"):
            entry["failed_since"] = _iso(_now_from(self.clock))
        now = _now_from(self.clock)
        if kind == "auth":
            entry["state"] = "authentication_required"
            entry["unavailable_until"] = None
        elif kind in ("rate_limit", "usage_limit"):
            entry["state"] = ("rate_limited" if kind == "rate_limit"
                              else "usage_exhausted")
            wait = self._wait_seconds(entry, kind, retry_after_seconds, reset_at)
            entry["unavailable_until"] = _iso(now + _dt.timedelta(seconds=wait))
        elif kind in ("backend_unavailable", "timeout", "provider_error",
                      "interrupted", "unknown"):
            if entry["consecutive_failures"] >= 3:
                entry["state"] = "unavailable"
                wait = self._wait_seconds(entry, "backend_unavailable",
                                          retry_after_seconds, reset_at)
                entry["unavailable_until"] = _iso(now + _dt.timedelta(seconds=wait))
            else:
                entry["state"] = "degraded"
        self.save()
        return entry["state"]

    def _wait_seconds(self, entry, kind, retry_after_seconds, reset_at):
        """Explicit provider hints win; otherwise use observed recovery
        history; grow the estimate while the backend keeps failing."""
        if retry_after_seconds:
            return min(int(retry_after_seconds), MAX_RECOVERY)
        if reset_at:
            try:
                delta = (_dt.datetime.fromisoformat(reset_at)
                         - _now_from(self.clock)).total_seconds()
                if delta > 0:
                    return min(int(delta), MAX_RECOVERY)
            except ValueError:
                pass
        history = entry.get("recovery_history_seconds") or []
        base = (sum(history) / len(history)) if history \
            else DEFAULT_RECOVERY.get(kind, 600)
        growth = 1.5 ** max(0, entry.get("consecutive_failures", 1) - 1)
        return int(min(base * growth, MAX_RECOVERY))

    def mark_health_ok(self, backend):
        """Half-open -> available after a lightweight health check."""
        entry = self.entry(backend)
        if entry["state"] == "cooling":
            entry["state"] = "available"
            self.save()

    def render(self):
        lines = ["%-14s %-24s %-20s %s" % ("BACKEND", "STATE",
                                           "UNAVAILABLE_UNTIL", "FAILS")]
        for backend in sorted(self.data):
            entry = self.data[backend]
            lines.append("%-14s %-24s %-20s %d" % (
                backend, entry["state"], entry.get("unavailable_until") or "-",
                entry.get("consecutive_failures", 0)))
        if len(lines) == 1:
            lines.append("(no backends recorded yet)")
        return "\n".join(lines)


def _now_from(clock):
    value = clock()
    return value if isinstance(value, _dt.datetime) else _dt.datetime.now()
