"""Budget enforcement, unknown-price behaviour, secret redaction, trust
promotion/demotion, goal violation."""
import datetime
import os

import pytest

from conftest import Transport, oai_body
from core import errors
from core.budget import Budget
from core.invoke import invoke_model
from core.redact import looks_like_secret, redact
from core.trust import TrustLedger


# 10. budget enforcement ---------------------------------------------------------
def _seed_spend(memory_dir, usd):
    os.makedirs(memory_dir, exist_ok=True)
    from core.budget import USAGE_COLUMNS
    with open(os.path.join(memory_dir, "usage.tsv"), "w", encoding="utf-8") as fh:
        fh.write("\t".join(USAGE_COLUMNS) + "\n")
        fh.write("\t".join([datetime.datetime.now().isoformat(), "r1", "triage",
                            "p", "m", "10", "10", "0", str(usd), "config",
                            "ok"]) + "\n")


def test_daily_budget_blocks_run_and_call(base_cfg, tmp_path):
    memory = str(tmp_path / "memory")
    _seed_spend(memory, 5.0)   # limit is 5
    budget = Budget(base_cfg, memory, "r2")
    with pytest.raises(errors.BudgetExceededError):
        budget.check_before_run()
    transport = Transport([(200, oai_body("hi"))])
    resp = invoke_model(base_cfg, "triage", "x", budget=budget,
                        transport=transport)
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "budget_exceeded"
    assert transport.calls == []   # blocked BEFORE the invocation


def test_budget_warning_threshold(base_cfg, tmp_path):
    memory = str(tmp_path / "memory")
    _seed_spend(memory, 4.5)   # 90% of the 5 USD limit
    budget = Budget(base_cfg, memory, "r2")
    budget.check_before_run()
    assert any("80" in w or "9" in w for w in budget.warnings)


# 11. unknown-price behaviour -----------------------------------------------------
def test_unknown_price_policy_block(base_cfg, tmp_path):
    base_cfg["providers"]["paid"] = {"type": "openai_compatible",
                                     "base_url": "http://p.local/v1",
                                     "api_key_required": False}
    base_cfg["roles"]["triage"]["provider"] = "paid"
    budget = Budget(base_cfg, str(tmp_path / "m"), "r")
    transport = Transport([(200, oai_body("hi"))])
    resp = invoke_model(base_cfg, "triage", "x", budget=budget,
                        transport=transport)
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "budget_exceeded"
    assert "unknown_price_policy" in resp["error"]["detail"]
    assert transport.calls == []


def test_unknown_price_policy_warn_allows_with_zero_cost(base_cfg, tmp_path):
    base_cfg["budget"]["unknown_price_policy"] = "warn"
    base_cfg["providers"]["paid"] = {"type": "openai_compatible",
                                     "base_url": "http://p.local/v1",
                                     "api_key_required": False}
    base_cfg["roles"]["triage"]["provider"] = "paid"
    budget = Budget(base_cfg, str(tmp_path / "m"), "r")
    resp = invoke_model(base_cfg, "triage", "x", budget=budget,
                        transport=Transport([(200, oai_body("hi"))]))
    assert resp["ok"] is True
    assert resp["estimated_cost_usd"] == 0.0
    assert budget.warnings


def test_configured_pricing_computes_cost(base_cfg, tmp_path):
    base_cfg["pricing"] = {"mock": {"default": {"input": 1.0, "output": 2.0}}}
    base_cfg["providers"]["mock"]["cost_free"] = False
    budget = Budget(base_cfg, str(tmp_path / "m"), "r")
    resp = invoke_model(base_cfg, "triage", "x", budget=budget,
                        transport=Transport([(200, oai_body("hi", in_tok=1000000,
                                                            out_tok=500000))]))
    assert resp["estimated_cost_usd"] == pytest.approx(1.0 + 1.0)


# 12. secret redaction ------------------------------------------------------------
def test_redaction_of_patterns_and_env_values(monkeypatch):
    monkeypatch.setenv("SOMETHING_API_KEY", "super-secret-value-123")
    text = ("key=sk-abcdefghijklmnop1234 env=super-secret-value-123 "
            "gh=ghp_ABCDEFGHIJKLMNOPQRST12 ok=hello")
    out = redact(text)
    assert "sk-abcdefghijklmnop1234" not in out
    assert "super-secret-value-123" not in out
    assert "ghp_" not in out
    assert "ok=hello" in out
    assert looks_like_secret("Authorization: Bearer abcdefghij1234567890")
    assert not looks_like_secret("plain text")


# 15. trust promotion -------------------------------------------------------------
def test_trust_promotion_to_auto(base_cfg, tmp_path):
    ledger = TrustLedger(base_cfg, str(tmp_path / "m"))
    for _ in range(20):
        ledger.record("fix-lint-debt", True)
    assert ledger.tier("fix-lint-debt") == "auto"


def test_intermediate_tier_is_queue(base_cfg, tmp_path):
    ledger = TrustLedger(base_cfg, str(tmp_path / "m"))
    for _ in range(12):
        ledger.record("s", True)
    assert ledger.tier("s") == "queue"   # >=10 runs, >=90%, not yet auto


def test_sensitive_skill_never_auto(base_cfg, tmp_path):
    base_cfg["trust"]["sensitive_skills"] = ["touch-auth"]
    ledger = TrustLedger(base_cfg, str(tmp_path / "m"))
    for _ in range(25):
        ledger.record("touch-auth", True)
    assert ledger.tier("touch-auth") == "queue"
    base_cfg["trust"]["sensitive_auto_allowed"] = ["touch-auth"]
    ledger2 = TrustLedger(base_cfg, str(tmp_path / "m"))
    ledger2.record("touch-auth", True)
    assert ledger2.tier("touch-auth") == "auto"   # explicit contract permission


# 16. trust demotion --------------------------------------------------------------
def test_two_consecutive_failures_force_watch(base_cfg, tmp_path):
    ledger = TrustLedger(base_cfg, str(tmp_path / "m"))
    for _ in range(30):
        ledger.record("fix-lint-debt", True)
    assert ledger.tier("fix-lint-debt") == "auto"
    ledger.record("fix-lint-debt", False)
    before, after = ledger.record("fix-lint-debt", False)
    assert after == "watch"

    # history survives a reload (changing model/provider never erases trust)
    reloaded = TrustLedger(base_cfg, str(tmp_path / "m"))
    assert reloaded.rows["fix-lint-debt"]["total_runs"] == 32


# 17. goal violation --------------------------------------------------------------
def test_goal_violation_detected(base_cfg, tmp_path):
    from core import goals
    agentic = tmp_path / "agentic"
    (agentic / "goals").mkdir(parents=True)
    (agentic / "memory").mkdir()
    goals.propose_goal(str(agentic), "always-fails", "demo",
                       "python -c \"import sys; sys.exit(1)\"")
    goals.propose_goal(str(agentic), "always-passes", "demo",
                       "python -c \"import sys; sys.exit(0)\"")
    violations, results = goals.check_goals(base_cfg, str(agentic), str(tmp_path))
    assert [v["id"] for v in violations] == ["always-fails"]
    assert len(results) == 2
    ledger = (agentic / "memory" / "goal-ledger.tsv").read_text(encoding="utf-8")
    assert "violation" in ledger and "pass" in ledger


def test_goal_requires_deterministic_predicate(tmp_path):
    from core import goals
    (tmp_path / "goals").mkdir()
    with pytest.raises(ValueError):
        goals.propose_goal(str(tmp_path), "vague", "be nicer", "")
