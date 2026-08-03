"""Deterministic active-canary replay over real persisted evidence."""
import json
import os

import pytest

from core.featuregates import FeatureGateRegistry
from core.featureprobe import run_contract_amendment_probe


def _seed(tmp_path, active=True):
    runtime = tmp_path / "runtime"
    memory = runtime / "memory"
    run = runtime / "runs" / "cycle-shadow-1"
    run.mkdir(parents=True)
    cfg = {
        "runtime": {"project_dir": str(runtime)},
        "advanced_features": {
            "contract_amendments": {
                "state": "canary" if active else "shadow",
                "canary_projects": ["probe-project"] if active else [],
            }},
    }
    ledger = {
        "policy": {"enabled": True,
                   "allowed_kinds": ["acceptance_criterion"],
                   "allowed_paths": [], "allowed_commands": []},
        "proposals": [{"id": "stronger-check",
                       "kind": "acceptance_criterion",
                       "value": "Whitespace-only input returns zero.",
                       "reason": "strengthens existing empty-input rule"}],
    }
    task_contract = {
        "task_id": "t3-interface",
        "required_outputs": [{"path": "index.html", "type": "file"}],
        "acceptance_criteria": ["Empty input returns zero."],
        "deterministic_checks": ["npm test"],
        "allowed_paths": ["index.html"],
    }
    (run / "contract-amendments.json").write_text(
        json.dumps(ledger), encoding="utf-8")
    (run / "task-contract.json").write_text(
        json.dumps(task_contract), encoding="utf-8")
    return cfg, memory


def test_probe_replays_real_proposal_and_preserves_live_canary(tmp_path):
    cfg, memory = _seed(tmp_path)

    result = run_contract_amendment_probe(cfg, "probe-project")

    assert result["passed"] is True
    assert result["accepted_count"] == 1
    assert result["applied"] == [{
        "id": "stronger-check", "kind": "acceptance_criterion",
        "value": "Whitespace-only input returns zero.", "applied": True}]
    assert result["rollback_guard"]["passed"] is True
    assert result["rollback_guard"]["resulting_state"] == "shadow"
    assert result["mutated_project_state"] is False
    live = FeatureGateRegistry(memory, cfg).decision(
        "contract_amendments", "probe-project")
    assert live["effective_state"] == "canary"
    assert live["active"] is True
    assert os.path.exists(result["report_path"])


def test_probe_refuses_inactive_feature(tmp_path):
    cfg, _memory = _seed(tmp_path, active=False)

    with pytest.raises(ValueError, match="is not active"):
        run_contract_amendment_probe(cfg, "probe-project")
