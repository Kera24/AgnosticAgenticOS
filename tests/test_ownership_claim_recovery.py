"""Recovery of canonical file ownership left by completed tasks."""
from core import projstate, recovery, taskspace


def test_done_task_claim_and_derived_blocker_are_recovered(tmp_path):
    agentic = str(tmp_path / "agentic")
    tasks = [
        projstate.normalize_task({
            "id": "t3-form", "milestone": "m1", "description": "form",
            "status": "done", "last_result": "pass",
        }),
        projstate.normalize_task({
            "id": "t4-wire", "milestone": "m1", "description": "wire",
            "status": "blocked", "last_result": "failure",
            "blocking_reason":
                "file ownership overlap: task t3-form already claims paths "
                "intersecting ['index.html']",
        }),
    ]
    projstate.save_backlog(agentic, tasks)
    projstate.write_yaml(agentic, "blockers.yaml", {"blockers": [{
        "task": "t4-wire",
        "reason": tasks[1]["blocking_reason"],
        "resolved": False,
    }]})
    taskspace.claim_paths(agentic, "t3-form", ["index.html"], run_id="r1")

    events = recovery._task_state_reconciliation(agentic)

    assert taskspace.active_claims(agentic) == {}
    current = {t["id"]: t for t in projstate.load_backlog(agentic)}
    assert current["t4-wire"]["status"] == "pending"
    assert current["t4-wire"]["blocking_reason"] is None
    blockers = projstate.read_yaml(agentic, "blockers.yaml", {})["blockers"]
    assert blockers[0]["resolved"] is True
    assert {event["action"] for event in events} == {
        "release_done_task_ownership_claim",
        "reset_stale_ownership_blocker",
    }
