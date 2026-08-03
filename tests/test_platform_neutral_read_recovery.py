"""Recovery for documentation blocked by Unix-only cat on Windows."""
import json

from conftest import project_cfg, seed_project, simple_task
from core import projstate, recovery


def test_recovery_retries_legacy_unix_only_read_check(sandbox):
    project_cfg(sandbox)
    task = simple_task("t10-readme-creation")
    seed_project(sandbox, [task])
    agentic = str(sandbox["agentic"])
    reason = "repeated identical failure (same diff, same errors)"
    projstate.update_task(agentic, task["id"], status="blocked",
                          blocking_reason=reason, last_result="failure")
    projstate.add_blocker(agentic, task["id"], reason, human_only=False,
                          code=None)

    cycle = sandbox["agentic"] / "runs" / "cycle-r1"
    cycle.mkdir(parents=True)
    (cycle / "work-order.json").write_text(json.dumps({
        "task_id": task["id"],
    }), encoding="utf-8")
    (cycle / "validation-result-1.json").write_text(json.dumps({
        "results": [{
            "name": "task-deterministic-1",
            "command": "cat README.md",
            "mandatory": True,
            "passed": False,
            "skipped_unix_only": True,
        }],
    }), encoding="utf-8")

    events = recovery.recover_platform_neutral_read_blocker(agentic)

    assert events[0]["action"] == (
        "reset_platform_neutral_read_check_blocker")
    updated = {item["id"]: item
               for item in projstate.load_backlog(agentic)}[task["id"]]
    assert updated["status"] == "pending"
    assert not projstate.open_blockers(agentic)
