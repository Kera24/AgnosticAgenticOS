"""Token and cost budgets. Real usage metadata is used when the provider
reports it; otherwise tokens are estimated (chars/4) and marked as such.
Prices come exclusively from config (`pricing:`) because prices change;
unknown prices follow budget.unknown_price_policy (block|warn|allow)."""
import datetime as _dt
import os

from . import errors

USAGE_COLUMNS = ["timestamp", "run_id", "role", "provider", "model",
                 "input_tokens", "output_tokens", "cached_tokens",
                 "estimated_cost_usd", "pricing_source", "status"]


def estimate_tokens(text):
    """Crude local fallback when a provider reports no usage."""
    return max(1, len(text or "") // 4)


class Budget:
    def __init__(self, cfg, memory_dir, run_id="adhoc"):
        self.cfg = cfg
        self.b = cfg.get("budget", {}) or {}
        self.run_id = run_id
        self.usage_path = os.path.join(memory_dir, "usage.tsv")
        self.run_cost = 0.0
        self.run_input_tokens = 0
        self.run_output_tokens = 0
        self.warnings = []
        # Set when a POST-call check detects exhaustion. The completed call's
        # result is preserved; the NEXT pre-call check stops the run safely.
        self.exhausted_reason = None

    # -- pricing -----------------------------------------------------------
    def price_entry(self, provider_name, provider_cfg, model):
        """Return (entry_or_None, source). entry = {input, output, cached}
        in USD per 1M tokens."""
        if (provider_cfg or {}).get("cost_free"):
            return {"input": 0, "output": 0, "cached": 0}, "cost_free"
        table = (self.cfg.get("pricing") or {}).get(provider_name) or {}
        entry = table.get(model) or table.get("default")
        if entry:
            return entry, "config"
        return None, "unknown"

    def cost_of(self, entry, usage):
        if entry is None:
            return 0.0
        per_m = 1_000_000.0
        cached = usage.get("cached_tokens", 0)
        uncached_in = max(0, usage.get("input_tokens", 0) - cached)
        return (uncached_in * float(entry.get("input", 0)) / per_m
                + cached * float(entry.get("cached", entry.get("input", 0))) / per_m
                + usage.get("output_tokens", 0) * float(entry.get("output", 0)) / per_m)

    # -- ledger ------------------------------------------------------------
    def _rows(self):
        if not os.path.exists(self.usage_path):
            return []
        rows = []
        with open(self.usage_path, "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == len(USAGE_COLUMNS) and parts[0] != "timestamp":
                    rows.append(dict(zip(USAGE_COLUMNS, parts)))
        return rows

    def daily_spend(self):
        today = _dt.date.today().isoformat()
        return sum(float(r["estimated_cost_usd"] or 0) for r in self._rows()
                   if r["timestamp"].startswith(today))

    def record(self, role, provider, model, usage, cost, pricing_source, status):
        os.makedirs(os.path.dirname(self.usage_path), exist_ok=True)
        new = not os.path.exists(self.usage_path)
        with open(self.usage_path, "a", encoding="utf-8", newline="") as fh:
            if new:
                fh.write("\t".join(USAGE_COLUMNS) + "\n")
            fh.write("\t".join(str(x) for x in [
                _dt.datetime.now().isoformat(timespec="seconds"), self.run_id,
                role, provider, model,
                usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                usage.get("cached_tokens", 0), "%.6f" % cost, pricing_source,
                status]) + "\n")
        self.run_cost += cost
        self.run_input_tokens += usage.get("input_tokens", 0)
        self.run_output_tokens += usage.get("output_tokens", 0)

    # -- enforcement -------------------------------------------------------
    def _limit(self, key, default):
        try:
            return float(self.b.get(key, default))
        except (TypeError, ValueError):
            return default

    def check_before_run(self):
        if self.exhausted_reason:
            raise errors.BudgetExceededError(self.exhausted_reason)
        self._check_daily("before run")

    def _check_daily(self, when):
        daily_limit = self._limit("daily_limit_usd", 5)
        spent = self.daily_spend() + self.run_cost
        if spent >= daily_limit:
            raise errors.BudgetExceededError(
                "daily budget exhausted (%.4f/%.2f USD, %s)" % (spent, daily_limit, when))
        warn_pct = self._limit("warning_percentage", 80)
        if daily_limit and spent >= daily_limit * warn_pct / 100.0:
            self.warnings.append(
                "daily budget at %.0f%% (%.4f/%.2f USD)"
                % (100.0 * spent / daily_limit, spent, daily_limit))

    def check_before_call(self, provider_name, provider_cfg, model, role):
        """Runs before every invocation, including fallbacks."""
        if self.exhausted_reason:
            raise errors.BudgetExceededError(self.exhausted_reason)
        self._check_daily("before call for role %s" % role)
        if self.run_cost >= self._limit("per_run_limit_usd", 2):
            raise errors.BudgetExceededError(
                "per-run budget exhausted (%.4f USD)" % self.run_cost)
        if self.run_input_tokens >= self._limit("max_input_tokens_per_run", 500000):
            raise errors.BudgetExceededError("per-run input token limit reached")
        if self.run_output_tokens >= self._limit("max_output_tokens_per_run", 50000):
            raise errors.BudgetExceededError("per-run output token limit reached")
        entry, source = self.price_entry(provider_name, provider_cfg, model)
        policy = str(self.b.get("unknown_price_policy", "block"))
        if entry is None and policy == "block":
            raise errors.BudgetExceededError(
                "no configured price for %s/%s and unknown_price_policy=block"
                % (provider_name, model))
        if entry is None and policy == "warn":
            self.warnings.append("price unknown for %s/%s; cost recorded as 0"
                                 % (provider_name, model))
        return entry, source

    def settle(self, response, entry, source, role, status="ok", prompt_text=None):
        """Attach cost to a normalized response and log it."""
        usage = response.get("usage") or {}
        if not any(usage.values()):
            usage = {"input_tokens": estimate_tokens(prompt_text),
                     "output_tokens": estimate_tokens(response.get("content", "")),
                     "cached_tokens": 0}
            source = source + "+token_estimate"
        cost = self.cost_of(entry, usage)
        response["estimated_cost_usd"] = round(cost, 6)
        self.record(role, response.get("provider", "?"),
                    response.get("model", "?"), usage, cost, source, status)
        # Post-call limit checks must never destroy the already-paid-for
        # result or crash the run: record exhaustion, stop at the next gate.
        try:
            self._check_daily("after call")
        except errors.BudgetExceededError as exc:
            self.exhausted_reason = exc.detail
            self.warnings.append("budget exhausted after call: %s" % exc.detail)
        return response
