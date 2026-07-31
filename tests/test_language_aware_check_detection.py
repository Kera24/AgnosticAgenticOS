"""Language-aware deterministic check detection and recovery."""
import json

from conftest import project_cfg, seed_project, simple_task
from core import gate, projstate, recovery


def _names(commands):
    return {item["name"] for item in commands}


def test_node_tests_directory_does_not_imply_pytest(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "app.test.js").write_text(
        "import assert from 'node:assert';\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"test": "node --test"},
    }), encoding="utf-8")

    commands = gate.detect_commands(str(tmp_path))

    assert _names(commands) == {"npm-test"}


def test_python_test_source_still_enables_pytest(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "def test_ok(): assert True\n", encoding="utf-8")

    commands = gate.detect_commands(str(tmp_path))

    assert _names(commands) == {"pytest"}


def test_recovery_resets_foreign_pytest_autodetection_blocker(sandbox):
    project_cfg(sandbox)
    task = simple_task(
        "t1-project-foundation",
        deterministic_checks=["npm test"])
    seed_project(sandbox, [task])
    agentic_dir = str(sandbox["agentic"])
    reason = ("pytest is outside the permitted Node-only check scope for "
              "this task")
    projstate.update_task(
        agentic_dir, task["id"], status="blocked",
        blocking_reason=reason, last_result="failure")
    projstate.add_blocker(
        agentic_dir, task["id"], reason, human_only=False, code=None)

    run_dir = sandbox["agentic"] / "runs" / "cycle-node1"
    run_dir.mkdir(parents=True)
    (run_dir / "task-contract.json").write_text(json.dumps({
        "task_id": task["id"],
        "run_id": "node1",
        "deterministic_checks": ["npm test"],
    }), encoding="utf-8")
    evidence_path = run_dir / "validation-result-1.json"
    evidence_path.write_text(json.dumps({
        "auto_detected": True,
        "results": [{
            "name": "pytest",
            "command": "python -m pytest -q",
            "passed": False,
            "exit_code": 5,
            "detail": "no tests ran in 0.02s",
        }],
    }), encoding="utf-8")

    events = recovery.recover_foreign_pytest_autodetection_blocker(
        agentic_dir)

    assert events[0]["action"] == \
        "reset_foreign_pytest_autodetection_blocker"
    assert events[0]["run_id"] == "node1"
    current = {item["id"]: item
               for item in projstate.load_backlog(agentic_dir)}[task["id"]]
    assert current["status"] == "pending"
    assert current["blocking_reason"] is None
    assert projstate.open_blockers(agentic_dir) == []


def test_recovery_does_not_clear_task_that_requires_pytest(sandbox):
    project_cfg(sandbox)
    task = simple_task(
        "t-python", deterministic_checks=["python -m pytest -q"])
    seed_project(sandbox, [task])
    agentic_dir = str(sandbox["agentic"])
    projstate.update_task(
        agentic_dir, task["id"], status="blocked",
        blocking_reason="real pytest failure", last_result="failure")

    run_dir = sandbox["agentic"] / "runs" / "cycle-python1"
    run_dir.mkdir(parents=True)
    (run_dir / "task-contract.json").write_text(json.dumps({
        "task_id": task["id"],
        "run_id": "python1",
        "deterministic_checks": ["python -m pytest -q"],
    }), encoding="utf-8")
    (run_dir / "validation-result-1.json").write_text(json.dumps({
        "auto_detected": True,
        "results": [{
            "name": "pytest", "exit_code": 5,
            "detail": "no tests ran in 0.01s",
        }],
    }), encoding="utf-8")

    assert recovery.recover_foreign_pytest_autodetection_blocker(
        agentic_dir) == []
    current = projstate.load_backlog(agentic_dir)[0]
    assert current["status"] == "blocked"
