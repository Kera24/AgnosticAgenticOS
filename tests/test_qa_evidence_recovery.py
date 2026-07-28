"""Recovery for QA evidence hidden by the legacy boolean-only payload."""
import json

from conftest import project_cfg, seed_project, simple_task
from core import projstate, recovery


def test_recovery_retries_filter_coverage_when_named_checks_already_pass(
        sandbox):
    project_cfg(sandbox)
    task = simple_task("t9-test-suite-setup")
    seed_project(sandbox, [task])
    agentic = str(sandbox["agentic"])
    reason = (
        "QA: The deterministic checks pass, but the required filter coverage "
        "is not satisfied.")
    projstate.update_task(agentic, task["id"], status="blocked",
                          blocking_reason=reason, last_result="failure")
    projstate.add_blocker(agentic, task["id"], reason, human_only=False,
                          code=None)

    cycle = sandbox["agentic"] / "runs" / "cycle-r1"
    cycle.mkdir(parents=True)
    (cycle / "task-contract.json").write_text(json.dumps({
        "task_id": task["id"], "run_id": "r1",
    }), encoding="utf-8")
    (cycle / "validation-result-1.json").write_text(json.dumps({
        "results": [{
            "name": "npm-test", "mandatory": True, "passed": True,
            "detail": (
                "Subtest: filters tasks by all, active, and completed"),
        }],
    }), encoding="utf-8")

    events = recovery.recover_qa_semantic_evidence_blocker(agentic)

    assert events[0]["action"] == "reset_qa_semantic_evidence_blocker"
    assert events[0]["run_id"] == "r1"
    updated = {item["id"]: item
               for item in projstate.load_backlog(agentic)}[task["id"]]
    assert updated["status"] == "pending"
    assert not projstate.open_blockers(agentic)
