"""Per-skill trust ledger. Trust is earned by skill name, not by model, so
changing providers never erases history. Tiers:

  watch : < 10 runs or pass rate < 90%  -> drafts only
  queue : intermediate                  -> verified work waits for approval
  auto  : >= 20 runs and >= 95%         -> may act autonomously (mode=auto)

Two consecutive failures force watch. Sensitive skills are capped at queue
unless listed in trust.sensitive_auto_allowed."""
import datetime as _dt
import os

COLUMNS = ["skill", "total_runs", "passes", "failures", "consecutive_failures",
           "pass_rate", "current_tier", "last_result", "updated_at"]

TIER_WATCH, TIER_QUEUE, TIER_AUTO = "watch", "queue", "auto"


class TrustLedger:
    def __init__(self, cfg, memory_dir):
        self.cfg = cfg
        self.tcfg = cfg.get("trust", {}) or {}
        self.path = os.path.join(memory_dir, "trust.tsv")
        self.by_model_path = os.path.join(memory_dir, "trust-by-model.tsv")
        self.rows = self._load()

    def _load(self):
        rows = {}
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) == len(COLUMNS) and parts[0] != "skill":
                        row = dict(zip(COLUMNS, parts))
                        for k in ("total_runs", "passes", "failures",
                                  "consecutive_failures"):
                            row[k] = int(row[k])
                        row["pass_rate"] = float(row["pass_rate"])
                        rows[row["skill"]] = row
        return rows

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            fh.write("\t".join(COLUMNS) + "\n")
            for skill in sorted(self.rows):
                r = self.rows[skill]
                fh.write("\t".join(str(r[c]) for c in COLUMNS) + "\n")

    def is_sensitive(self, skill):
        return skill in (self.tcfg.get("sensitive_skills") or [])

    def compute_tier(self, row, sensitive=False):
        runs, rate = row["total_runs"], row["pass_rate"]
        if row["consecutive_failures"] >= 2:
            return TIER_WATCH
        if runs < 10 or rate < 0.90:
            return TIER_WATCH
        allowed_sensitive = self.tcfg.get("sensitive_auto_allowed") or []
        if runs >= 20 and rate >= 0.95:
            if sensitive and row["skill"] not in allowed_sensitive:
                return TIER_QUEUE
            return TIER_AUTO
        return TIER_QUEUE

    def tier(self, skill):
        row = self.rows.get(skill)
        return row["current_tier"] if row else TIER_WATCH

    def record(self, skill, passed, provider=None, model=None):
        """Log one completed attempt. A deterministic-gate failure is a
        failure. Returns (tier_before, tier_after) so demotions can alert."""
        row = self.rows.setdefault(skill, {
            "skill": skill, "total_runs": 0, "passes": 0, "failures": 0,
            "consecutive_failures": 0, "pass_rate": 0.0,
            "current_tier": TIER_WATCH, "last_result": "-", "updated_at": "-"})
        before = row["current_tier"]
        row["total_runs"] += 1
        if passed:
            row["passes"] += 1
            row["consecutive_failures"] = 0
        else:
            row["failures"] += 1
            row["consecutive_failures"] += 1
        row["pass_rate"] = round(row["passes"] / row["total_runs"], 4)
        row["last_result"] = "pass" if passed else "fail"
        row["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        row["current_tier"] = self.compute_tier(row, self.is_sensitive(skill))
        self.save()
        if self.tcfg.get("track_by_model") and provider and model:
            self._record_by_model(skill, passed, provider, model)
        return before, row["current_tier"]

    def _record_by_model(self, skill, passed, provider, model):
        new = not os.path.exists(self.by_model_path)
        with open(self.by_model_path, "a", encoding="utf-8", newline="") as fh:
            if new:
                fh.write("timestamp\tskill\tprovider\tmodel\tresult\n")
            fh.write("%s\t%s\t%s\t%s\t%s\n" % (
                _dt.datetime.now().isoformat(timespec="seconds"), skill,
                provider, model, "pass" if passed else "fail"))

    def render(self):
        lines = ["%-28s %5s %5s %5s %6s %-6s %s" %
                 ("SKILL", "RUNS", "PASS", "FAIL", "RATE", "TIER", "UPDATED")]
        for skill in sorted(self.rows):
            r = self.rows[skill]
            lines.append("%-28s %5d %5d %5d %5.0f%% %-6s %s" % (
                skill, r["total_runs"], r["passes"], r["failures"],
                r["pass_rate"] * 100, r["current_tier"], r["updated_at"]))
        if len(lines) == 1:
            lines.append("(no skills recorded yet)")
        return "\n".join(lines)
