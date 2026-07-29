"""Bounded historical task evidence for the final reviewer."""
import json

from core import project


def test_historical_task_evidence_uses_latest_successful_task_gate(tmp_path):
    old = tmp_path / "cycle-r1"
    new = tmp_path / "cycle-r2"
    old.mkdir()
    new.mkdir()
    for directory, run_id in ((old, "r1"), (new, "r2")):
        (directory / "task-contract.json").write_text(json.dumps({
            "task_id": "t6-filter-controls", "run_id": run_id,
            "acceptance_criteria": ["filters work"],
            "deterministic_checks": ["node filter-test.js"],
        }), encoding="utf-8")
    (old / "validation-result-1.json").write_text(json.dumps({
        "ok": True, "results": [{
            "name": "task-deterministic-1",
            "command": "node filter-test.js", "mandatory": True,
            "passed": True, "detail": "old evidence",
        }],
    }), encoding="utf-8")
    (new / "validation-result-1.json").write_text(json.dumps({
        "ok": True, "results": [{
            "name": "task-deterministic-1",
            "command": "node filter-test.js", "mandatory": True,
            "passed": True,
            "detail": "All Active Completed filter buttons passed",
        }, {
            "name": "npm-test", "command": "npm test",
            "mandatory": True, "passed": True, "detail": "suite noise",
        }],
    }), encoding="utf-8")

    evidence = project._historical_task_evidence(str(tmp_path))

    assert evidence == [{
        "task_id": "t6-filter-controls",
        "run_id": "r2",
        "acceptance_criteria": ["filters work"],
        "checks": [{
            "name": "task-deterministic-1",
            "command": "node filter-test.js",
            "passed": True,
            "detail": "All Active Completed filter buttons passed",
        }],
    }]


def test_historical_task_evidence_ignores_failed_validation(tmp_path):
    cycle = tmp_path / "cycle-r1"
    cycle.mkdir()
    (cycle / "task-contract.json").write_text(json.dumps({
        "task_id": "t1", "deterministic_checks": ["bad"],
    }), encoding="utf-8")
    (cycle / "validation-result-1.json").write_text(json.dumps({
        "ok": False, "results": [{
            "name": "task-deterministic-1", "command": "bad",
            "mandatory": True, "passed": False, "detail": "failed",
        }],
    }), encoding="utf-8")

    assert project._historical_task_evidence(str(tmp_path)) == []
