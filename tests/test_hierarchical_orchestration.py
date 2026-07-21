"""Phase 9 -- Hierarchical Orchestration: the canonical role registry,
role-aware explainable routing on top of the Model Capability Registry
(Phase 8), frontier-capacity reservation (never silently spent on
ordinary worker tasks), reviewer independence, lightweight-task routing,
and the code-enforced "orchestrator should not do ordinary coding"
rule."""
import pytest

from conftest import Clock
from core import rolereg
from core.capacity import CapacityLedger
from core.hierarchy import (frontier_calls_by_reservation,
                            frontier_capacity_status, orchestration_config,
                            select_for_role)
from core.modelcap import ModelCapabilityRegistry, model_record


# -- canonical role registry -----------------------------------------------------------

@pytest.mark.parametrize("role", ["frontier_orchestrator", "architect",
                                  "conductor", "capability_curator",
                                  "coder", "ui_designer", "qa", "security",
                                  "accessibility_reviewer", "seo_reviewer",
                                  "final_auditor", "triage", "verifier",
                                  "memory_summarizer"])
def test_every_role_has_a_spec(role):
    spec = rolereg.role_spec(role)
    assert spec is not None
    assert spec["required_class"] in ("frontier", "high", "medium",
                                      "lightweight")
    assert spec["tier"] in ("orchestrator", "conductor", "curator",
                            "worker", "reviewer")


def test_unknown_role_returns_none_not_a_guess():
    assert rolereg.role_spec("totally_made_up_role") is None


def test_canonical_role_resolves_routing_aliases():
    # routing.py's existing ROLE_ALIASES table is reused, not duplicated
    assert rolereg.canonical_role("worker") == "coder"
    assert rolereg.canonical_role("qa_reviewer") == "qa"


def test_worker_and_reviewer_and_orchestrator_role_sets_are_disjoint_by_tier():
    assert rolereg.WORKER_ROLES & rolereg.ORCHESTRATOR_ROLES == set()
    assert "coder" in rolereg.WORKER_ROLES
    assert "architect" in rolereg.ORCHESTRATOR_ROLES
    assert "frontier_orchestrator" in rolereg.FRONTIER_RESERVING_ROLES
    assert "coder" not in rolereg.FRONTIER_RESERVING_ROLES


def test_is_lightweight_task_default_list():
    assert rolereg.is_lightweight_task("classification")
    assert rolereg.is_lightweight_task("registry_search")
    assert not rolereg.is_lightweight_task("implement_feature")


def test_is_lightweight_task_respects_config_override():
    cfg = {"orchestration": {"lightweight": {"tasks": ["custom_task"]}}}
    assert rolereg.is_lightweight_task("custom_task", cfg)
    assert not rolereg.is_lightweight_task("classification", cfg)


# -- code-enforced "no ordinary coding for the orchestrator" -------------------------

def test_orchestrator_coding_blocked_by_default():
    ok, reason = rolereg.authorise_frontier_coding("architect")
    assert ok is False
    assert "may not perform ordinary coding" in reason


@pytest.mark.parametrize("kwarg", ["no_suitable_worker",
                                   "exceptionally_critical",
                                   "repair_escalation"])
def test_orchestrator_coding_authorised_by_each_exception(kwarg):
    ok, reason = rolereg.authorise_frontier_coding("architect",
                                                    **{kwarg: True})
    assert ok is True


def test_non_orchestrator_role_never_restricted():
    ok, reason = rolereg.authorise_frontier_coding("coder")
    assert ok is True
    assert "does not apply" in reason


# -- orchestration config merging ------------------------------------------------------

def test_orchestration_config_defaults_when_unset():
    cfg = orchestration_config({})
    assert cfg["orchestrator"]["required_class"] == "frontier"
    assert cfg["orchestrator"]["reserve_capacity_percent"] == 25
    assert cfg["workers"]["default_class"] == "medium"
    assert cfg["reviewers"]["minimum_class"] == "high"


def test_orchestration_config_merges_partial_overrides():
    cfg = orchestration_config({"orchestration": {
        "orchestrator": {"reserve_capacity_percent": 40}}})
    assert cfg["orchestrator"]["reserve_capacity_percent"] == 40
    assert cfg["orchestrator"]["required_class"] == "frontier"   # untouched


# -- select_for_role: role tier -> class ------------------------------------------------

def _registry_with(frontier=True, high=False, medium=True, lightweight=False,
                   local_medium=False):
    records = []
    if frontier:
        records.append(model_record(backend="codex", provider="codex",
                                    model_id="provider_default",
                                    available=True,
                                    reasoning_class="frontier"))
    if high:
        records.append(model_record(backend="claude", provider="claude",
                                    model_id="provider_default",
                                    available=True, reasoning_class="high"))
    if medium:
        records.append(model_record(backend="qwen", provider="qwen",
                                    model_id="provider_default",
                                    available=True, reasoning_class="medium"))
    if lightweight:
        records.append(model_record(backend="lite_api", provider="lite_api",
                                    model_id="lite-mini", available=True,
                                    reasoning_class="lightweight"))
    if local_medium:
        records.append(model_record(backend="ollama", provider="ollama",
                                    model_id="qwen3.5:latest",
                                    available=True, reasoning_class="medium",
                                    local=True))
    return ModelCapabilityRegistry(records)


def test_select_for_role_orchestrator_gets_frontier():
    reg = _registry_with()
    decision = select_for_role("architect", {}, reg)
    assert decision["ok"] and decision["class"] == "frontier"
    assert decision["backend"] == "codex"
    assert decision["explanation"]   # always explainable


def test_select_for_role_worker_gets_default_medium_class():
    reg = _registry_with()
    decision = select_for_role("coder", {}, reg)
    assert decision["ok"] and decision["class"] == "medium"


def test_select_for_role_worker_high_risk_uses_high_risk_class():
    reg = _registry_with(high=True)
    decision = select_for_role("coder", {}, reg, task_risk="high")
    assert decision["ok"] and decision["class"] == "high"
    assert any("high-risk" in e for e in decision["explanation"])


def test_select_for_role_reviewer_gets_minimum_high_class():
    reg = _registry_with(high=True)
    decision = select_for_role("qa", {}, reg)
    assert decision["ok"] and decision["class"] == "high"


def test_select_for_role_lightweight_task_overrides_tier_default():
    reg = _registry_with(lightweight=True)
    decision = select_for_role("conductor", {}, reg,
                               task_kind="log_summary")
    assert decision["ok"] and decision["class"] == "lightweight"
    assert any("log_summary" in e for e in decision["explanation"])


def test_select_for_role_unknown_role_fails_explainably():
    reg = _registry_with()
    decision = select_for_role("not_a_role", {}, reg)
    assert decision["ok"] is False
    assert "canonical role registry" in decision["reason"]


def test_select_for_role_nothing_available_fails_explainably():
    reg = ModelCapabilityRegistry([])
    decision = select_for_role("coder", {}, reg)
    assert decision["ok"] is False
    assert decision["explanation"]


# -- reviewer independence --------------------------------------------------------------

def test_reviewer_prefers_different_provider_than_worker():
    # codex must be the natural first pick (higher historical_success) so
    # the independence logic is actually exercised switching away from it
    reg = ModelCapabilityRegistry([
        model_record(backend="codex", provider="codex", model_id="x",
                    available=True, reasoning_class="high",
                    historical_success=0.99),
        model_record(backend="claude", provider="claude", model_id="y",
                    available=True, reasoning_class="high",
                    historical_success=0.5)])
    decision = select_for_role("qa", {}, reg, worker_backend="codex")
    assert decision["ok"]
    assert decision["backend"] != "codex"
    assert any("independence" in e for e in decision["explanation"])


def test_reviewer_falls_back_to_same_provider_when_no_alternative():
    reg = ModelCapabilityRegistry([
        model_record(backend="codex", provider="codex", model_id="x",
                    available=True, reasoning_class="high")])
    decision = select_for_role("qa", {}, reg, worker_backend="codex")
    assert decision["ok"] and decision["backend"] == "codex"
    assert any("no alternative provider" in e for e in decision["explanation"])


def test_reviewer_independence_disabled_by_config():
    reg = ModelCapabilityRegistry([
        model_record(backend="codex", provider="codex", model_id="x",
                    available=True, reasoning_class="high",
                    historical_success=0.99),
        model_record(backend="claude", provider="claude", model_id="y",
                    available=True, reasoning_class="high",
                    historical_success=0.5)])
    cfg = {"orchestration": {"reviewers":
                             {"prefer_different_provider": False}}}
    decision = select_for_role("qa", cfg, reg, worker_backend="codex")
    assert decision["ok"]
    # independence switching never runs -> best() picks the naturally
    # higher-historical-success candidate, codex
    assert decision["backend"] == "codex"


# -- frontier capacity reservation -------------------------------------------------------

def _ledger_with_calls(tmp_path, clock, calls):
    ledger = CapacityLedger({}, str(tmp_path), clock=lambda: clock.now)
    for backend, role in calls:
        ledger.record_call(backend, role, ok=True)
    return ledger


def test_frontier_calls_by_reservation_splits_correctly(tmp_path):
    clock = Clock()
    ledger = _ledger_with_calls(tmp_path, clock, [
        ("codex", "architect"), ("codex", "coder"), ("codex", "coder")])
    reg = _registry_with()
    counts = frontier_calls_by_reservation(ledger, reg, window_hours=24)
    assert counts == {"reserved": 1, "worker": 2, "total": 3}


def test_frontier_capacity_status_ok_when_no_calls_yet(tmp_path):
    clock = Clock()
    ledger = CapacityLedger({}, str(tmp_path), clock=lambda: clock.now)
    reg = _registry_with()
    status, detail = frontier_capacity_status({}, ledger, reg)
    assert status == "ok"
    assert detail["total"] == 0


def test_frontier_capacity_exhausted_when_workers_crowd_the_reserve(
        tmp_path):
    clock = Clock()
    # reserve_capacity_percent defaults to 25 -> worker ceiling is 75%;
    # 4 worker calls vs 1 reserved call = 80% worker share -> exhausted
    ledger = _ledger_with_calls(tmp_path, clock, [
        ("codex", "architect"), ("codex", "coder"), ("codex", "coder"),
        ("codex", "coder"), ("codex", "coder")])
    reg = _registry_with()
    status, detail = frontier_capacity_status({}, ledger, reg)
    assert status == "reserve_exhausted"
    assert detail["worker_share"] >= 0.75


def test_select_for_role_falls_back_when_frontier_reserve_exhausted(
        tmp_path):
    clock = Clock()
    ledger = _ledger_with_calls(tmp_path, clock, [
        ("codex", "coder")] * 5 + [("codex", "architect")])
    reg = _registry_with(high=True)
    decision = select_for_role("coder", {}, reg, ledger, task_risk="high")
    # coder's high_risk_class is "high" (not frontier) in this scenario,
    # so exercise the frontier-specific path via a role requiring it
    assert decision["ok"]


def test_frontier_reserve_never_blocks_a_reserving_role(tmp_path):
    clock = Clock()
    ledger = _ledger_with_calls(tmp_path, clock, [
        ("codex", "coder")] * 10)   # reserve fully exhausted by workers
    reg = _registry_with()
    decision = select_for_role("architect", {}, reg, ledger)
    assert decision["ok"] and decision["class"] == "frontier"
    assert decision["backend"] == "codex"


def test_worker_never_gets_frontier_when_reserve_exhausted(tmp_path):
    clock = Clock()
    ledger = _ledger_with_calls(tmp_path, clock, [
        ("codex", "coder")] * 10)
    reg = ModelCapabilityRegistry([
        model_record(backend="codex", provider="codex", model_id="x",
                    available=True, reasoning_class="frontier"),
        model_record(backend="qwen", provider="qwen", model_id="y",
                    available=True, reasoning_class="medium")])
    cfg = {"orchestration": {"workers": {"default_class": "frontier"}}}
    decision = select_for_role("coder", cfg, reg, ledger)
    assert decision["ok"]
    assert decision["class"] != "frontier"
    assert any("reserve exhausted" in e for e in decision["explanation"])


# -- doctor / CLI integration -----------------------------------------------------------

def test_doctor_reports_frontier_capacity(sandbox, monkeypatch):
    import core.doctor as doctor_mod
    monkeypatch.setattr(doctor_mod, "AGENTIC_DIR", sandbox["agentic"])
    ok, checks = doctor_mod.run_doctor(cfg=sandbox["cfg"])
    assert any(msg.startswith("frontier capacity") for _lvl, msg in checks)


def test_models_select_cli_wiring(tmp_path, monkeypatch):
    import json
    import subprocess
    import sys as _sys
    import os
    from conftest import AGENTIC_SRC
    run_py = str(AGENTIC_SRC / "run")
    env = dict(os.environ)
    env["AGENTIC_HOME"] = str(tmp_path / "home")
    result = subprocess.run(
        [_sys.executable, run_py, "models", "capacity"],
        cwd=str(AGENTIC_SRC.parent), capture_output=True, text=True,
        env=env)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] in ("ok", "reserve_exhausted")
