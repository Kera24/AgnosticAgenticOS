"""Recovery for QA evidence hidden by the legacy boolean-only payload."""
import json

from conftest import Clock, project_cfg, seed_project, simple_task
from core import logs, projstate, recovery
from core.scheduler import Scheduler


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



def test_full_recovery_removes_corrected_qa_evidence_failure_from_streak(
        sandbox):
    project_cfg(sandbox)
    task = simple_task("t9-test-suite-setup")
    seed_project(sandbox, [task])
    agentic = str(sandbox["agentic"])
    memory = str(sandbox["agentic"] / "memory")
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
            "detail": "filters tasks by all, active, and completed",
        }],
    }), encoding="utf-8")
    logs.decision(memory, {
        "event": "cycle_finished", "run_id": "r1", "outcome": "failure",
        "detail": "QA verdict fail after 3 review rounds",
    })
    scheduler = Scheduler(sandbox["cfg"], memory, clock=Clock())
    scheduler.state["failure_streak"] = 1
    scheduler.save()

    report = recovery.run_recovery(
        sandbox["cfg"], agentic, str(sandbox["repo"]), scheduler, Clock(),
        log=lambda event: None)

    streak = report["failure_streak_reconciliation"]
    assert streak["previous_streak"] == 1
    assert streak["resulting_streak"] == 0
    assert [item["run_id"] for item in streak["removed_platform_failures"]] == [
        "r1"]



def test_recovery_resets_task_worktree_that_predates_filter_feature(
        sandbox, monkeypatch):
    project_cfg(sandbox)
    task = simple_task("t9-test-suite-setup")
    seed_project(sandbox, [task])
    agentic = str(sandbox["agentic"])
    reason = (
        "Required real all/active/completed filter behavior is absent from "
        "repository source, and the work order only allows editing "
        "package.json and tests/*.test.js.")
    projstate.update_task(agentic, task["id"], status="blocked",
                          blocking_reason=reason, last_result="failure")
    projstate.add_blocker(agentic, task["id"], reason, human_only=False,
                          code=None)

    project_index = (sandbox["agentic"] / "worktrees" / "project" /
                     "src" / "index.js")
    task_index = (sandbox["agentic"] / "worktrees" / "tasks" / task["id"] /
                  "src" / "index.js")
    project_index.parent.mkdir(parents=True)
    task_index.parent.mkdir(parents=True)
    project_index.write_text(
        "const activeFilter='all'; button.getAttribute('data-task-filter');",
        encoding="utf-8")
    task_index.write_text("export function render() {}", encoding="utf-8")

    archived = []
    monkeypatch.setattr(
        recovery.taskspace, "archive_and_reset_task_worktree",
        lambda root, agentic_dir, task_id, evidence_id: (
            archived.append((task_id, evidence_id)) or
            {"archived_branch": "agentic/evidence/t9-r1"}))
    memory = str(sandbox["agentic"] / "memory")
    scheduler = Scheduler(sandbox["cfg"], memory, clock=Clock())
    scheduler.state["current_cycle"] = "r1"
    scheduler.save()

    events = recovery.recover_stale_filter_source_blocker(
        agentic, str(sandbox["repo"]), scheduler)

    assert archived == [(task["id"], "r1")]
    assert events[0]["action"] == (
        "archive_stale_recovered_task_worktree_and_retry")
    updated = {item["id"]: item
               for item in projstate.load_backlog(agentic)}[task["id"]]
    assert updated["status"] == "pending"
    assert not projstate.open_blockers(agentic)
