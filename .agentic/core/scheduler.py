"""Persistent cycle scheduler.

State lives in .agentic/memory/scheduler.json and survives process/computer
restarts. Long waits are never a blocking sleep: the next eligible run time
is persisted, `project-run` exits, and any timer (cron / systemd / Task
Scheduler — installed only by explicit user action) simply re-invokes
`project-run`, which continues where it left off.

Cooling policy (after a WHOLE cycle, never between agents inside one):
- success  -> cooling.after_success_minutes   (default 30)
- failure  -> cooling.after_failure_minutes   (default 30)
- rate limit / usage exhaustion -> dynamic, from explicit provider hints or
  the circuit-breaker's historical recovery estimate
- everything clamped to [minimum_minutes, maximum_minutes] (5..360)
"""
import datetime as _dt
import json
import os

DEFAULT_COOLING = {"after_success_minutes": 30, "after_failure_minutes": 30,
                   "after_platform_failure_minutes": 5,
                   "minimum_minutes": 5, "maximum_minutes": 360,
                   "adaptive": True}


def _now():
    return _dt.datetime.now()


class Scheduler:
    def __init__(self, cfg, memory_dir, clock=None):
        self.cfg = cfg
        self.path = os.path.join(memory_dir, "scheduler.json")
        self.clock = clock or _now
        self.state = self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as fh:
                    return json.load(fh)
            except ValueError:
                pass
        return {"state": "idle", "current_cycle": None, "next_run_at": None,
                "cooling_reason": None, "selected_backend": None,
                "paused_backends": [], "project_status": "none",
                "last_heartbeat": None, "failure_streak": 0,
                "deferred": None}

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.state["last_heartbeat"] = self.clock().isoformat(
            timespec="seconds")
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2)
        os.replace(tmp, self.path)

    # -- cooling ------------------------------------------------------------
    def _cooling_cfg(self):
        merged = dict(DEFAULT_COOLING)
        # Load the deprecated top-level shape first. The canonical nested
        # scheduler.cooling setting (including machine-local overrides)
        # must win when both are present.
        merged.update(self.cfg.get("cooling") or {})
        merged.update((self.cfg.get("scheduler") or {}).get("cooling") or {})
        return merged

    def _clamp(self, minutes):
        cooling = self._cooling_cfg()
        return max(float(cooling.get("minimum_minutes", 5)),
                   min(float(minutes),
                       float(cooling.get("maximum_minutes", 360))))

    def cooldown_breakdown(self, outcome, retry_after_seconds=None,
                           breaker_wait_seconds=None, failure_streak=0):
        """Same number `cooldown_minutes` returns, plus a transparent
        explanation of how it was computed -- there is no hidden
        hard-coded value anywhere in this calculation, only: the
        configured base (`scheduler.cooling.after_failure_minutes`,
        default 30), an exponential-backoff multiplier applied ONLY when
        `dynamic`/`adaptive` is enabled (default on) AND this is at
        least the second consecutive failure, OR a capacity/rate-limit
        hint for provider-side outcomes, OR the short, NEVER-escalating
        `after_platform_failure_minutes` base for a failure the platform
        itself classified as its own (see core.failures) rather than the
        model's or the provider's -- then the configured
        [minimum_minutes, maximum_minutes] clamp. `source` names exactly
        which of those produced the number."""
        cooling = self._cooling_cfg()
        detail = {
            "outcome": outcome,
            "configured_after_success_minutes":
                float(cooling.get("after_success_minutes", 30)),
            "configured_after_failure_minutes":
                float(cooling.get("after_failure_minutes", 30)),
            "configured_after_platform_failure_minutes":
                float(cooling.get("after_platform_failure_minutes", 5)),
            "dynamic_backoff_enabled":
                bool(cooling.get("dynamic", cooling.get("adaptive", True))),
            "failure_streak": failure_streak, "backoff_multiplier": 1.0,
            "minimum_minutes": float(cooling.get("minimum_minutes", 5)),
            "maximum_minutes": float(cooling.get("maximum_minutes", 360)),
            "source": None, "base_minutes": None, "raw_minutes": None,
            "clamped_minutes": None,
        }
        if outcome == "success":
            detail["source"] = "configured_success_cooldown"
            minutes = detail["base_minutes"] = \
                detail["configured_after_success_minutes"]
        elif outcome in ("rate_limit", "usage_limit"):
            if retry_after_seconds:
                detail["source"] = "provider_retry_after_hint"
                minutes = retry_after_seconds / 60.0
            elif breaker_wait_seconds:
                detail["source"] = "circuit_breaker_recovery_estimate"
                minutes = breaker_wait_seconds / 60.0
            else:
                detail["source"] = "capacity_default"
                minutes = 60.0 if outcome == "usage_limit" else 15.0
            detail["base_minutes"] = minutes
        elif outcome == "platform_failure":
            # a defect in the platform's own contract/capability/policy
            # layer, never the model's or provider's fault: short, FLAT
            # cooldown -- never the exponential backoff below, which
            # exists to protect a struggling MODEL/PROVIDER, not to
            # penalise the platform for its own (often already-fixed-by-
            # recovery) bug.
            detail["source"] = "configured_platform_failure_cooldown"
            minutes = detail["base_minutes"] = \
                detail["configured_after_platform_failure_minutes"]
        else:   # ordinary failure; consecutive failures escalate (adaptive)
            detail["source"] = "configured_failure_cooldown"
            minutes = detail["base_minutes"] = \
                detail["configured_after_failure_minutes"]
            if detail["dynamic_backoff_enabled"] and failure_streak > 1:
                exponent = min(failure_streak - 1, 3)
                detail["backoff_multiplier"] = float(2 ** exponent)
                detail["source"] = ("configured_failure_cooldown+"
                                    "exponential_backoff")
                minutes *= detail["backoff_multiplier"]
        detail["raw_minutes"] = minutes
        detail["clamped_minutes"] = self._clamp(minutes)
        return detail

    def cooldown_minutes(self, outcome, retry_after_seconds=None,
                         breaker_wait_seconds=None, failure_streak=0):
        return self.cooldown_breakdown(
            outcome, retry_after_seconds, breaker_wait_seconds,
            failure_streak)["clamped_minutes"]

    def start_cooling(self, outcome, retry_after_seconds=None,
                      breaker_wait_seconds=None):
        if outcome == "success":
            self.state["failure_streak"] = 0
        elif outcome not in ("rate_limit", "usage_limit", "platform_failure"):
            # a platform_failure never touches the streak: it is not
            # evidence the MODEL or PROVIDER is struggling, so it must
            # never feed the exponential backoff that protects them.
            self.state["failure_streak"] = \
                int(self.state.get("failure_streak") or 0) + 1
        breakdown = self.cooldown_breakdown(
            outcome, retry_after_seconds, breaker_wait_seconds,
            failure_streak=int(self.state.get("failure_streak") or 0))
        minutes = breakdown["clamped_minutes"]
        until = self.clock() + _dt.timedelta(minutes=minutes)
        self.state.update(state="cooling", cooling_reason=outcome,
                          cooling_detail=breakdown,
                          next_run_at=until.isoformat(timespec="seconds"),
                          deferred=None)
        self.save()
        return until

    def defer(self, reason, required_estimated_tokens=None, confidence=None,
              until=None):
        """Persist WHY the next cycle is not starting (capacity shortfall,
        unavailable backends, …) plus the estimate behind the decision."""
        self.state.update(
            state="cooling", cooling_reason="capacity",
            next_run_at=until,
            deferred={"reason": reason,
                      "required_estimated_tokens": required_estimated_tokens,
                      "capacity_confidence": confidence,
                      "next_eligible": until,
                      "recorded_at": self.clock().isoformat(
                          timespec="seconds")})
        self.save()

    # -- eligibility -----------------------------------------------------------
    def in_operating_window(self):
        window = (self.cfg.get("scheduler") or {}).get("operating_window") or {}
        if not window.get("enabled"):
            return True
        now = self.clock().time()
        try:
            start = _dt.time.fromisoformat(str(window.get("start", "00:00")))
            stop = _dt.time.fromisoformat(str(window.get("stop", "23:59")))
        except ValueError:
            return True
        if start <= stop:
            return start <= now <= stop
        return now >= start or now <= stop   # overnight window

    def window_minutes_remaining(self):
        """Minutes until the operating window closes; None when the window
        is disabled (unbounded)."""
        window = (self.cfg.get("scheduler") or {}).get("operating_window") \
            or {}
        if not window.get("enabled"):
            return None
        now = self.clock()
        try:
            stop = _dt.time.fromisoformat(str(window.get("stop", "23:59")))
        except ValueError:
            return None
        stop_dt = now.replace(hour=stop.hour, minute=stop.minute,
                              second=0, microsecond=0)
        if stop_dt < now:
            stop_dt += _dt.timedelta(days=1)
        return (stop_dt - now).total_seconds() / 60.0

    def eligible(self, cycle_minutes=None):
        """(eligible: bool, reason: str). Never blocks. When cycle_minutes
        is given, the whole cycle envelope must fit in the remaining
        operating window."""
        if self.state["state"] == "paused":
            return False, "paused by user"
        if self.state["state"] == "complete":
            return False, "project complete"
        if not self.in_operating_window():
            return False, "outside operating window"
        if cycle_minutes:
            remaining = self.window_minutes_remaining()
            if remaining is not None and remaining < float(cycle_minutes):
                return False, ("insufficient operating window: %.0f min "
                               "left, cycle needs %s" % (remaining,
                                                         cycle_minutes))
        next_run = self.state.get("next_run_at")
        if next_run:
            try:
                when = _dt.datetime.fromisoformat(next_run)
            except ValueError:
                when = None
            if when and self.clock() < when:
                return False, "cooling until %s (%s)" % (
                    next_run, self.state.get("cooling_reason"))
        return True, "eligible"

    # -- state changes ------------------------------------------------------------
    def begin_cycle(self, run_id, backend):
        self.state.update(state="running", current_cycle=run_id,
                          selected_backend=backend, next_run_at=None,
                          cooling_reason=None)
        self.save()

    def pause(self):
        self.state.update(state="paused")
        self.save()

    def resume(self, force=False):
        """Unpause; with force=True (explicit user override) also clear any
        cooling wait so the next cycle may start immediately."""
        if self.state["state"] == "paused":
            self.state.update(state="idle")
            self.save()
        if force and self.state["state"] in ("cooling", "idle"):
            self.state.update(state="idle", next_run_at=None,
                              cooling_reason=None, deferred=None)
            self.save()

    def mark_complete(self):
        self.state.update(state="complete", project_status="complete",
                          next_run_at=None)
        self.save()

    def set_project_status(self, status):
        self.state["project_status"] = status
        self.save()

    def render(self):
        return json.dumps(self.state, indent=2)
