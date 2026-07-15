"""Protected-path rejection, prompt-injection resistance at the policy
boundary, and the deterministic gate."""
from core import gate, gitops
from core.orchestrator import apply_policy
from core.trust import TrustLedger

PROTECTED = [".env", ".env.*", "**/auth/**", "**/secrets/**",
             ".github/workflows/**", ".agentic/core/**"]


# 13. protected-path rejection ----------------------------------------------------
def test_check_paths_rejects_protected_and_out_of_scope():
    violations = gitops.check_paths(
        [".env", "src/auth/login.py", "src/ok.py", "docs/readme.md"],
        allowed=["src/**", "docs/**"], forbidden=["docs/**"],
        protected=PROTECTED)
    assert any(".env" in v and "protected" in v for v in violations)
    assert any("auth/login.py" in v and "protected" in v for v in violations)
    assert any("docs/readme.md" in v and "forbidden" in v for v in violations)
    assert not any("src/ok.py" in v for v in violations)


def test_empty_allowed_paths_denies_everything():
    assert gitops.check_paths(["anything.py"], [], [], PROTECTED)


def test_safe_join_blocks_traversal(tmp_path):
    import pytest
    from core import errors
    with pytest.raises(errors.PolicyError):
        gitops.safe_join(str(tmp_path), "../outside.txt")
    with pytest.raises(errors.PolicyError):
        gitops.safe_join(str(tmp_path), "/abs/path")


# 22. prompt-injection resistance at the policy boundary ---------------------------
def _ledger(cfg, tmp_path):
    return TrustLedger(cfg, str(tmp_path / "m"))


def test_injected_order_cannot_grant_protected_paths(base_cfg, tmp_path):
    from conftest import order_out
    # a poisoned finding talked the conductor into requesting .env access
    order = order_out(allowed_paths=[".env", "src/app.py"])
    decision = apply_policy(base_cfg, order, _ledger(base_cfg, tmp_path), PROTECTED)
    assert decision["action"] == "queue"
    assert any("protected" in r for r in decision["reasons"])


def test_injected_wildcard_scope_is_queued(base_cfg, tmp_path):
    from conftest import order_out
    order = order_out(allowed_paths=["**"])
    decision = apply_policy(base_cfg, order, _ledger(base_cfg, tmp_path), PROTECTED)
    assert decision["action"] == "queue"


def test_oversized_high_risk_and_dependency_orders_queue(base_cfg, tmp_path):
    from conftest import order_out
    ledger = _ledger(base_cfg, tmp_path)
    assert apply_policy(base_cfg, order_out(maximum_changed_lines=100000),
                        ledger, PROTECTED)["action"] == "queue"
    assert apply_policy(base_cfg, order_out(risk="high"),
                        ledger, PROTECTED)["action"] == "queue"
    assert apply_policy(base_cfg, order_out(allowed_paths=["package.json"]),
                        ledger, PROTECTED)["action"] == "queue"
    assert apply_policy(base_cfg, order_out(done_when=[]),
                        ledger, PROTECTED)["action"] == "queue"


def test_clean_order_executes(base_cfg, tmp_path):
    from conftest import order_out
    decision = apply_policy(base_cfg, order_out(), _ledger(base_cfg, tmp_path),
                            PROTECTED)
    assert decision == {"action": "execute", "reasons": []}


# 18. failed deterministic gate ------------------------------------------------
FAIL_CMD = {"name": "always-fails",
            "command": "python -c \"import sys; sys.exit(1)\"",
            "mandatory": True}
PASS_CMD = {"name": "always-passes",
            "command": "python -c \"import sys; sys.exit(0)\"",
            "mandatory": True}


def test_gate_fails_on_mandatory_failure(base_cfg, tmp_path):
    base_cfg["verification"]["commands"] = [PASS_CMD, FAIL_CMD]
    base_cfg["verification"]["fail_fast"] = False
    result = gate.run_checks(base_cfg, str(tmp_path), str(tmp_path / "logs"))
    assert result["ok"] is False
    assert [r["passed"] for r in result["results"]] == [True, False]
    # logs preserved
    assert (tmp_path / "logs" / "always-fails.log").exists()


def test_baseline_tolerates_preexisting_failure_but_not_regressions(
        base_cfg, tmp_path):
    agentic = tmp_path / "agentic"
    (agentic / "memory").mkdir(parents=True)
    base_cfg["verification"]["commands"] = [FAIL_CMD]
    result = gate.run_checks(base_cfg, str(tmp_path))
    gate.save_baseline(str(agentic), result["results"])

    verdict = gate.evaluate_against_baseline(str(agentic), result)
    assert verdict["ok"] is True                 # known failure, no regression
    assert verdict["fully_healthy"] is False     # never claimed healthy
    assert verdict["known_failing"] == ["always-fails"]

    # a NEW failing check is a regression
    base_cfg["verification"]["commands"] = [FAIL_CMD,
                                            dict(FAIL_CMD, name="new-check")]
    base_cfg["verification"]["fail_fast"] = False
    result2 = gate.run_checks(base_cfg, str(tmp_path))
    verdict2 = gate.evaluate_against_baseline(str(agentic), result2)
    assert verdict2["ok"] is False
    assert verdict2["regressions"] == ["new-check"]
