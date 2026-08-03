"""Phase 3: promotion-gated advanced capability execution."""
import json

import pytest

from conftest import simple_task
from core import contract
from core.featuregates import FeatureGateRegistry


def _cfg(state="disabled", projects=None):
    return {
        "advanced_features": {
            "contract_amendments": {
                "state": state,
                "canary_projects": list(projects or []),
            }}}


def test_feature_is_disabled_by_default(tmp_path):
    registry = FeatureGateRegistry(tmp_path, {})
    decision = registry.decision("contract_amendments", "project-a")

    assert decision["effective_state"] == "disabled"
    assert decision["active"] is False
    assert decision["observe_only"] is False


def test_shadow_mode_observes_without_applying_amendment(tmp_path):
    registry = FeatureGateRegistry(tmp_path, _cfg("shadow"))
    gate = registry.decision("contract_amendments", "project-a")
    task = simple_task(
        "t-shadow",
        expected_paths=[{"path": "src/base.py", "type": "file"}],
        contract_amendment_policy={
            "enabled": True,
            "allowed_kinds": ["required_output"],
            "allowed_paths": ["generated/**"],
        })
    order = {
        "allowed_paths": ["src/base.py"],
        "contract_amendments": [{
            "id": "report",
            "kind": "required_output",
            "value": "generated/report.json",
            "reason": "observe proposed output",
        }],
    }

    stable = contract.canonicalize_work_order(
        task, order, feature_gate=gate)

    assert stable["expected_outputs"] == ["src/base.py"]
    decision = stable["contract_amendment_decisions"][0]
    assert decision["accepted"] is False
    assert decision["would_accept"] is True
    assert decision["decision_code"] == "shadow_observation"


def test_canary_runs_only_for_selected_projects(tmp_path):
    registry = FeatureGateRegistry(
        tmp_path, _cfg("canary", ["selected-project"]))

    selected = registry.decision(
        "contract_amendments", "selected-project")
    other = registry.decision(
        "contract_amendments", "other-project")

    assert selected["active"] is True
    assert selected["canary_selected"] is True
    assert other["active"] is False
    assert other["canary_selected"] is False


def test_platform_failure_rolls_canary_back_to_shadow(tmp_path):
    registry = FeatureGateRegistry(
        tmp_path, _cfg("canary", ["project-a"]))
    gate = registry.decision("contract_amendments", "project-a")
    registry.begin_run("run-1", {"contract_amendments": gate})

    events = registry.record_outcome(
        "run-1", "failure", platform_failure=True,
        detail="platform integration regression")

    assert events == [{
        "feature": "contract_amendments",
        "action": "rollback_canary_to_shadow",
        "reason": "platform integration regression",
    }]
    after = registry.decision("contract_amendments", "project-a")
    assert after["effective_state"] == "shadow"
    assert after["active"] is False
    assert after["observe_only"] is True
    assert after["source"] == "runtime_rollback"
    assert registry.status("contract_amendments")["rollback_count"] == 1


def test_genuine_task_failure_does_not_rollback_canary(tmp_path):
    registry = FeatureGateRegistry(
        tmp_path, _cfg("canary", ["project-a"]))
    gate = registry.decision("contract_amendments", "project-a")
    registry.begin_run("run-1", {"contract_amendments": gate})

    assert registry.record_outcome(
        "run-1", "failure", platform_failure=False) == []

    after = registry.decision("contract_amendments", "project-a")
    assert after["effective_state"] == "canary"
    assert after["active"] is True
    assert registry.status("contract_amendments")["failures"] == 1


def test_outcome_recording_is_idempotent(tmp_path):
    registry = FeatureGateRegistry(tmp_path, _cfg("shadow"))
    gate = registry.decision("contract_amendments", "project-a")
    registry.begin_run("run-1", {"contract_amendments": gate})

    registry.record_outcome("run-1", "success")
    registry.record_outcome("run-1", "success")

    assert registry.status("contract_amendments")["successes"] == 1


def test_promotion_requires_successful_evidence(tmp_path):
    registry = FeatureGateRegistry(tmp_path, _cfg("shadow"))

    with pytest.raises(ValueError, match="insufficient successful evidence"):
        registry.promote(
            "contract_amendments", "stable", minimum_successes=1)

    gate = registry.decision("contract_amendments", "project-a")
    registry.begin_run("run-1", {"contract_amendments": gate})
    registry.record_outcome("run-1", "success")
    promoted = registry.promote(
        "contract_amendments", "canary", minimum_successes=1,
        project_id="project-a")
    registry.record_probe(
        "contract_amendments", "project-a", "probe-1", True)
    promoted = registry.promote(
        "contract_amendments", "stable", minimum_successes=1,
        project_id="project-a")

    assert promoted["effective_state"] == "stable"
    assert promoted["active"] is True


def test_stable_promotion_rejects_shadow_evidence_without_canary_probe(
        tmp_path):
    registry = FeatureGateRegistry(tmp_path, _cfg("shadow"))
    gate = registry.decision("contract_amendments", "project-a")
    registry.begin_run("shadow-1", {"contract_amendments": gate})
    registry.record_outcome("shadow-1", "success")
    registry.promote(
        "contract_amendments", "canary", minimum_successes=1,
        project_id="project-a")

    with pytest.raises(ValueError, match="canary probe evidence"):
        registry.promote(
            "contract_amendments", "stable", minimum_successes=1,
            project_id="project-a")


def test_probe_evidence_is_state_specific_and_idempotent(tmp_path):
    registry = FeatureGateRegistry(tmp_path, _cfg("shadow"))
    shadow = registry.decision("contract_amendments", "project-a")
    registry.begin_run("shadow-1", {"contract_amendments": shadow})
    registry.record_outcome("shadow-1", "success")
    registry.promote(
        "contract_amendments", "canary", minimum_successes=1,
        project_id="project-a")

    assert registry.record_probe(
        "contract_amendments", "project-a", "source-run", True) is True
    assert registry.record_probe(
        "contract_amendments", "project-a", "source-run", True) is False

    status = registry.status("contract_amendments", "project-a")
    assert status["successes_by_state"] == {"shadow": 1}
    assert status["canary_probe_successes"] == 1
    assert status["stable_promotion"]["eligible"] is True


def test_registry_evidence_persists_across_instances(tmp_path):
    first = FeatureGateRegistry(tmp_path, _cfg("shadow"))
    gate = first.decision("contract_amendments", "project-a")
    first.begin_run("run-1", {"contract_amendments": gate})
    first.record_outcome("run-1", "success")

    second = FeatureGateRegistry(tmp_path, _cfg("shadow"))

    assert second.status("contract_amendments")["successes"] == 1
    with open(second.path, encoding="utf-8") as handle:
        persisted = json.load(handle)
    assert persisted["runs"]["run-1"]["outcome_recorded"] is True


def test_legacy_success_totals_migrate_conservatively_to_shadow(tmp_path):
    path = tmp_path / "feature-gates.json"
    path.write_text(json.dumps({
        "version": 1,
        "features": {"contract_amendments": {
            "successes": 4, "failures": 0,
            "platform_failures": 0, "rollback_count": 0,
            "override_state": "canary", "override_source": "promotion",
            "runtime_canary_projects": ["project-a"],
            "last_rollback_reason": None,
        }},
        "runs": {},
    }), encoding="utf-8")

    status = FeatureGateRegistry(
        tmp_path, _cfg("shadow")).status(
            "contract_amendments", "project-a")

    assert status["successes_by_state"] == {"shadow": 4}
    assert status["canary_probe_successes"] == 0
    assert status["stable_promotion"]["eligible"] is False



def test_status_preserves_project_scope(tmp_path):
    registry = FeatureGateRegistry(
        tmp_path, _cfg("canary", ["project-a"]))

    status = registry.status(
        "contract_amendments", project_id="project-a")

    assert status["project_id"] == "project-a"
    assert status["canary_selected"] is True
    assert status["active"] is True



def test_runtime_canary_promotion_selects_only_target_project(tmp_path):
    registry = FeatureGateRegistry(tmp_path, _cfg("shadow"))
    gate = registry.decision("contract_amendments", "project-a")
    registry.begin_run("shadow-1", {"contract_amendments": gate})
    registry.record_outcome("shadow-1", "success")

    promoted = registry.promote(
        "contract_amendments", "canary", minimum_successes=1,
        project_id="project-a")

    assert promoted["effective_state"] == "canary"
    assert promoted["active"] is True
    assert promoted["source"] == "runtime_promotion"
    assert promoted["canary_projects"] == ["project-a"]
    assert registry.decision(
        "contract_amendments", "other-project")["active"] is False

    reloaded = FeatureGateRegistry(tmp_path, _cfg("shadow"))
    assert reloaded.decision(
        "contract_amendments", "project-a")["active"] is True


def test_canary_promotion_requires_explicit_project_scope(tmp_path):
    registry = FeatureGateRegistry(tmp_path, _cfg("shadow"))
    gate = registry.decision("contract_amendments", "project-a")
    registry.begin_run("shadow-1", {"contract_amendments": gate})
    registry.record_outcome("shadow-1", "success")

    with pytest.raises(ValueError, match="requires a project id"):
        registry.promote(
            "contract_amendments", "canary", minimum_successes=1)
