"""Regression coverage for semantic deterministic evidence sent to QA."""
from core import project


def test_review_input_preserves_bounded_check_command_and_detail(monkeypatch):
    monkeypatch.setattr(project.gitops, "changed_files",
                        lambda worktree: ["tests/task.test.js"])
    monkeypatch.setattr(project.gitops, "diff_text",
                        lambda worktree: "+test('filters all active completed')")

    detail = "named subtest: filters all active completed\n" + ("x" * 2000)
    payload = project._review_input(
        {"acceptance_criteria": ["filter coverage"]},
        "worktree",
        {"ok": True, "tests": "passed", "results": [{
            "name": "task-deterministic-1",
            "command": "npm test",
            "mandatory": True,
            "passed": True,
            "detail": detail,
        }]},
        task={"acceptance_criteria": ["filter coverage"]},
    )

    evidence = payload["deterministic_checks"]["results"][0]
    assert evidence["command"] == "npm test"
    assert evidence["detail"].startswith(
        "named subtest: filters all active completed")
    assert len(evidence["detail"]) == 1200
