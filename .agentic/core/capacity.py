"""Deterministic capacity manager.

Subscription CLIs rarely expose exact remaining quota, so every number here
carries a confidence label: `reported` (the provider said so), `estimated`
(derived from local history), or `unknown`. Estimated capacity is never
presented as a provider quota.

Ledger: .agentic/memory/capacity.tsv (one row per call) and
.agentic/memory/cycles.tsv (one row per completed cycle).
"""
import datetime as _dt
import os

CALL_COLUMNS = ["timestamp", "backend", "role", "ok", "event",
                "input_tokens", "cached_input_tokens", "output_tokens",
                "reasoning_tokens", "estimated", "duration_seconds"]
CYCLE_COLUMNS = ["timestamp", "run_id", "backend", "skill", "task_size",
                 "total_tokens", "duration_seconds", "result"]

DEFAULT_ROLE_TOKENS = {"conductor": 6000, "coder": 20000, "qa": 8000,
                       "security": 8000, "architect": 12000}
ORCHESTRATION_OVERHEAD_TOKENS = 2000
SIZE_FACTOR = {"small": 0.7, "medium": 1.0, "large": 1.6}


def estimate_tokens_from_text(text):
    return max(1, len(text or "") // 4)


class CapacityLedger:
    def __init__(self, cfg, memory_dir, clock=None):
        self.cfg = cfg
        self.memory_dir = memory_dir
        self.calls_path = os.path.join(memory_dir, "capacity.tsv")
        self.cycles_path = os.path.join(memory_dir, "cycles.tsv")
        self.clock = clock or _dt.datetime.now

    # -- recording -------------------------------------------------------------
    def _append(self, path, columns, row):
        os.makedirs(self.memory_dir, exist_ok=True)
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8", newline="") as fh:
            if new:
                fh.write("\t".join(columns) + "\n")
            fh.write("\t".join("" if row.get(c) is None else str(row[c])
                               for c in columns) + "\n")

    def record_call(self, backend, role, ok, usage=None, event="ok",
                    duration_seconds=0):
        usage = usage or {}
        self._append(self.calls_path, CALL_COLUMNS, {
            "timestamp": self.clock().isoformat(timespec="seconds"),
            "backend": backend, "role": role, "ok": int(bool(ok)),
            "event": event,
            "input_tokens": usage.get("input_tokens"),
            "cached_input_tokens": usage.get("cached_input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "reasoning_tokens": usage.get("reasoning_tokens"),
            "estimated": int(bool(usage.get("estimated", True))),
            "duration_seconds": duration_seconds})

    def record_cycle(self, run_id, backend, skill, task_size, total_tokens,
                     duration_seconds, result):
        self._append(self.cycles_path, CYCLE_COLUMNS, {
            "timestamp": self.clock().isoformat(timespec="seconds"),
            "run_id": run_id, "backend": backend, "skill": skill,
            "task_size": task_size, "total_tokens": total_tokens,
            "duration_seconds": duration_seconds, "result": result})

    # -- reading -----------------------------------------------------------------
    def _rows(self, path, columns):
        if not os.path.exists(path):
            return []
        rows = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == len(columns) and parts[0] != "timestamp":
                    rows.append(dict(zip(columns, parts)))
        return rows

    def calls_in_window(self, backend, hours):
        cutoff = self.clock() - _dt.timedelta(hours=hours)
        out = []
        for row in self._rows(self.calls_path, CALL_COLUMNS):
            try:
                ts = _dt.datetime.fromisoformat(row["timestamp"])
            except ValueError:
                continue
            if ts >= cutoff and row["backend"] == backend:
                out.append(row)
        return out

    def tokens_in_window(self, backend, hours):
        total = 0
        for row in self.calls_in_window(backend, hours):
            for col in ("input_tokens", "output_tokens", "reasoning_tokens"):
                if row.get(col):
                    try:
                        total += int(float(row[col]))
                    except ValueError:
                        pass
        return total

    def recent_cycles(self, backend=None, skill=None, limit=20):
        rows = self._rows(self.cycles_path, CYCLE_COLUMNS)
        if backend:
            rows = [r for r in rows if r["backend"] == backend]
        if skill:
            rows = [r for r in rows if r["skill"] == skill]
        return rows[-limit:]

    # -- self-imposed limits -------------------------------------------------------
    def limit_status(self, backend):
        """Check user-configured local limits. `null` limits mean the user has
        not set one — no provider limit is ever invented."""
        limits = (self.cfg.get("limits") or {}).get(backend) or {}
        reasons = []
        checks = [
            ("maximum_calls_per_hour", len(self.calls_in_window(backend, 1))),
            ("maximum_calls_per_day", len(self.calls_in_window(backend, 24))),
            ("maximum_estimated_tokens_per_hour",
             self.tokens_in_window(backend, 1)),
            ("maximum_estimated_tokens_per_day",
             self.tokens_in_window(backend, 24)),
        ]
        for key, used in checks:
            limit = limits.get(key)
            if limit is not None and used >= limit:
                reasons.append("%s reached (%s/%s)" % (key, used, limit))
        return reasons

    def remaining_by_limits(self, backend):
        """Remaining estimated tokens under the tightest configured local
        limit, or None when the user configured none."""
        limits = (self.cfg.get("limits") or {}).get(backend) or {}
        remaining = None
        for key, hours in (("maximum_estimated_tokens_per_hour", 1),
                           ("maximum_estimated_tokens_per_day", 24)):
            limit = limits.get(key)
            if limit is not None:
                left = max(0, int(limit) - self.tokens_in_window(backend, hours))
                remaining = left if remaining is None else min(remaining, left)
        return remaining


# -- next-cycle estimation ---------------------------------------------------------

def capacity_config(cfg):
    """Effective capacity policy: `capacity:` merged with the newer
    `scheduler.capacity:` overrides."""
    merged = {"safety_multiplier": 1.35, "stop_before_exhaustion": True,
              "include_review_reserve": True, "confidence_required": False}
    merged.update(cfg.get("capacity") or {})
    merged.update((cfg.get("scheduler") or {}).get("capacity") or {})
    return merged


def _safety_multiplier(cfg):
    raw = capacity_config(cfg).get("safety_multiplier", 1.35)
    try:
        return min(3.0, max(1.0, float(raw)))
    except (TypeError, ValueError):
        return 1.35


def estimate_cycle_tokens(cfg, task, ledger, backend):
    """Conservative next-cycle token estimate: context + expected output +
    review + repair reserve + overhead, scaled by size and history. The
    review/repair reserve is included unless explicitly disabled."""
    ccfg = capacity_config(cfg)
    size = (task or {}).get("expected_size", "medium")
    factor = SIZE_FACTOR.get(size, 1.0)
    security = bool((task or {}).get("security_relevant"))
    per_role = dict(DEFAULT_ROLE_TOKENS)
    per_role.update(ccfg.get("role_tokens") or {})

    # history for the same skill/backend refines the coder estimate
    history = ledger.recent_cycles(backend=backend,
                                   skill=(task or {}).get("skill"))
    hist_tokens = [int(float(r["total_tokens"])) for r in history
                   if r.get("total_tokens")]
    review_reserve = 0
    if ccfg.get("include_review_reserve", True):
        review_reserve = int(
            per_role["qa"] * factor
            + (per_role["security"] * factor if security else 0)
            + per_role["coder"] * factor * 0.5)     # repair reserve
    estimated = int(
        per_role["conductor"] * factor
        + per_role["coder"] * factor
        + review_reserve
        + ORCHESTRATION_OVERHEAD_TOKENS)
    highest_recent = max(hist_tokens) if hist_tokens else 0
    required = int(max(estimated, highest_recent) * _safety_multiplier(cfg))
    return {"estimated_cycle_tokens": estimated,
            "review_reserve_tokens": review_reserve,
            "highest_recent_cycle_tokens": highest_recent,
            "required_capacity_tokens": required,
            "safety_multiplier": _safety_multiplier(cfg),
            "history_samples": len(hist_tokens)}


def decide_start(cfg, task, ledger, board, chain, reported_remaining=None):
    """Deterministic start decision for the next cycle.

    chain: ordered backend names, primary first. Returns the documented
    decision dict; never claims estimated capacity as a provider quota."""
    ccfg = capacity_config(cfg)
    stop_short = bool(ccfg.get("stop_before_exhaustion", True))
    candidates, wait_untils = [], []
    estimate = None
    for backend in chain:
        estimate = estimate_cycle_tokens(cfg, task, ledger, backend)
        required = estimate["required_capacity_tokens"]
        if not board.is_available(backend):
            until = board.unavailable_until(backend)
            if until:
                wait_untils.append(until)
            continue
        limit_reasons = ledger.limit_status(backend)
        if limit_reasons:
            continue
        verb = "start" if backend == chain[0] else "reroute"
        reported = (reported_remaining or {}).get(backend)
        if reported is not None:
            if reported >= required or not stop_short:
                return _decision(verb, "reported", backend, required,
                                 reported, estimate, chain,
                                 "reported capacity sufficient"
                                 if reported >= required else
                                 "reported capacity SHORT of the estimated "
                                 "envelope; proceeding because "
                                 "stop_before_exhaustion is disabled")
            continue
        remaining = ledger.remaining_by_limits(backend)
        if remaining is not None:
            if remaining >= required or not stop_short:
                return _decision(verb, "estimated", backend, required,
                                 remaining, estimate, chain,
                                 "estimated capacity under local limits "
                                 "sufficient (estimate, not provider quota)"
                                 if remaining >= required else
                                 "estimated capacity short; proceeding "
                                 "because stop_before_exhaustion is "
                                 "disabled")
            continue
        # capacity unknown: rely on history and the safety reserve
        candidates.append((backend, required))
    if candidates:
        backend, required = candidates[0]
        if ccfg.get("confidence_required"):
            return _decision("human_required", "unknown", None, required,
                             None, estimate, chain,
                             "capacity confidence required but only "
                             "unknown-capacity backends are usable")
        decision = "start" if backend == chain[0] else "reroute"
        return _decision(decision, "unknown", backend, required, None,
                         estimate, chain,
                         "capacity unknown; proceeding conservatively with "
                         "increased safety reserve")
    if wait_untils:
        return _decision("wait", "estimated", None,
                         estimate["required_capacity_tokens"] if estimate else 0,
                         None, estimate, chain,
                         "no backend currently available",
                         wait_until=min(wait_untils))
    return _decision("human_required", "unknown", None,
                     estimate["required_capacity_tokens"] if estimate else 0,
                     None, estimate, chain,
                     "no configured backend is usable; human attention needed")


def _decision(decision, confidence, backend, required, available, estimate,
              chain, reason, wait_until=None):
    fallbacks = [b for b in chain if b != backend]
    return {"decision": decision, "confidence": confidence,
            "selected_backend": backend,
            "required_estimated_tokens": required,
            "available_estimated_tokens": available,
            "safety_reserve_tokens": int(
                required - (estimate or {}).get("estimated_cycle_tokens", required))
            if estimate else 0,
            "fallback_candidates": fallbacks, "wait_until": wait_until,
            "reason": reason}
