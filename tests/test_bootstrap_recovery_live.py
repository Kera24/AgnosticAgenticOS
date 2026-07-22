"""Live-regression tests reproducing the exact persisted state the
ollama-pilot project hit on its first real recovery attempt:

- two duplicate blockers for the same t1-init-repo failure (one
  human_only=True with the long reason, one human_only=False with the
  short reason -- the pre-fix duplicate-write bug in `fail()`);
- three reversible technical-choice "human decisions" (test framework /
  CSS approach / browser baseline) that were never genuine human
  decisions;
- a legacy backlog task with no `kind` field at all (created before that
  classification existed);
- scheduler project_status already latched to "blocked_on_human".

These must all self-heal on the NEXT cycle, in the right order, without
ever fabricating a completed task or corrupting unrelated state."""
import json

from conftest import Clock, FakeCaller, project_cfg, seed_project, simple_task
from core import bootstrap_gate, decision_policy, projstate
from core.project import run_cycle
from core.scheduler import Scheduler

TEST_FRAMEWORK_DECISION = "Choice of test framework (Jest/Vitest/Mocha)"
CSS_APPROACH_DECISION = ("CSS approach: vanilla CSS variables or BEM "
                         "methodology preference")
BROWSER_BASELINE_DECISION = ("Browser compatibility baseline for "
                             "ES2015+ features")
UNRELATED_HUMAN_DECISION = "obtain payment provider API credentials"


def _seed_live_ollama_pilot_state(sandbox, mode="completion_only",
                                  extra_decisions=None):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []   # the real pilot state
    sandbox["cfg"]["interaction"]["mode"] = mode
    task = simple_task("t1-init-repo")   # no `kind` -- predates the field
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    needed = [TEST_FRAMEWORK_DECISION, CSS_APPROACH_DECISION,
             BROWSER_BASELINE_DECISION] + list(extra_decisions or [])
    projstate.write_yaml(a, "decisions.yaml",
                         {"human_decisions_needed": needed, "decided": []})
    projstate.update_task(a, "t1-init-repo", status="blocked",
                          blocking_reason="no deterministic checks configured")
    ts = "2026-07-22T03:44:02"
    records = [{"task": None, "reason": text, "human_only": True,
               "created_at": ts, "resolved": False} for text in needed]
    records += [
        {"task": "t1-init-repo",
         "reason": "no deterministic checks configured; configure "
                   "verification.commands",
         "human_only": True, "created_at": ts, "resolved": False},
        {"task": "t1-init-repo", "reason": "no deterministic checks configured",
         "human_only": False, "created_at": ts, "resolved": False},
    ]
    projstate.write_yaml(a, "blockers.yaml", {"blockers": records})
    scheduler = Scheduler(sandbox["cfg"], str(sandbox["agentic"] / "memory"))
    scheduler.set_project_status("blocked_on_human")
    return task


def _std_caller(task):
    from conftest import proj_order, worker_out
    return FakeCaller({
        "conductor": proj_order(task),
        "coder": worker_out(),
        "qa": {"verdict": "pass", "done_when_results": [], "reason": "ok",
              "out_of_scope_changes": [], "test_integrity_preserved": True},
        "security": {"verdict": "pass", "concerns": [], "reason": "clean"},
    })


# 1/2/3/4/7/8/9 -- the full live scenario, one cycle -------------------------
def test_live_ollama_pilot_state_self_heals_and_reaches_backend(sandbox):
    task = _seed_live_ollama_pilot_state(sandbox)
    a = str(sandbox["agentic"])
    caller = _std_caller(task)

    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock(),
                       run_id="recovery-1")

    # 3 + 9: recovery ran before the human-blocker gate AND the cycle
    # actually reached backend invocation (Ollama-equivalent mock) --
    # NOT another human_required bounce.
    assert result["status"] == "success"
    assert [c["role"] for c in caller.calls if c["role"] == "conductor"]
    assert [c["role"] for c in caller.calls if c["role"] == "coder"]

    # 1/2: both the human_only and non-human duplicate deterministic-checks
    # blockers for t1-init-repo are resolved.
    blockers = projstate.read_yaml(a, "blockers.yaml", {}).get("blockers", [])
    det_blockers = [b for b in blockers if b.get("task") == "t1-init-repo"]
    assert len(det_blockers) == 2
    assert all(b["resolved"] for b in det_blockers)
    assert all(b["code"] == bootstrap_gate.DETERMINISTIC_CHECKS_MISSING_CODE
              for b in det_blockers)

    # 5: the three reversible technical choices were auto-resolved, not
    # left as human blockers.
    decisions_blockers = [b for b in blockers if b.get("task") is None]
    assert all(b["resolved"] for b in decisions_blockers)
    decisions = projstate.read_yaml(a, "decisions.yaml", {})
    assert decisions["human_decisions_needed"] == []
    decided_texts = {d["decision"] for d in decisions["decided"]}
    assert decided_texts == {TEST_FRAMEWORK_DECISION, CSS_APPROACH_DECISION,
                             BROWSER_BASELINE_DECISION}
    for record in decisions["decided"]:
        assert record["category"] == decision_policy.CATEGORY_REVERSIBLE
        assert record["resolution_method"] in (
            decision_policy.METHOD_AUTONOMOUS_DEFAULT,
            decision_policy.METHOD_INFERRED_FROM_REPOSITORY)
        assert record["value"]
        assert record["rationale"]

    # 7: no duplicate records were created by recovery -- same 5 as seeded.
    assert len(blockers) == 5

    # 10: the retry went through the structural bootstrap gate (still
    # reporting tests: not_configured_yet, never "passed") -- not a bare
    # AI-declared pass.
    log_path = sandbox["agentic"] / "memory" / "decisions.jsonl"
    events = [json.loads(line) for line in
             log_path.read_text(encoding="utf-8").splitlines() if line]
    gate_events = [e for e in events
                  if e.get("event") == "bootstrap_structural_gate"]
    assert gate_events and gate_events[-1]["tests"] == "not_configured_yet"

    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t1-init-repo"]["status"] == "done"
    assert tasks["t1-init-repo"]["last_result"] == "pass"
    assert tasks["t1-init-repo"]["kind"] == "bootstrap"

    scheduler = Scheduler(sandbox["cfg"], str(sandbox["agentic"] / "memory"))
    assert scheduler.state["project_status"] != "blocked_on_human"


# 8 (isolated): recovery alone never marks the task completed ---------------
def test_recovery_alone_returns_task_to_pending_never_done(sandbox):
    _seed_live_ollama_pilot_state(sandbox)
    a = str(sandbox["agentic"])
    events = bootstrap_gate.recover_bootstrap_deadlock(a)
    assert [e["task_id"] for e in events] == ["t1-init-repo"]
    task = next(t for t in projstate.load_backlog(a)
               if t["id"] == "t1-init-repo")
    assert task["status"] == "pending"
    assert task["status"] != "done"
    assert task["blocking_reason"] is None


# 3/4: recovery runs before the human-blocker gate, and never sweeps up a
# genuinely unrelated human decision along with it -----------------------------
def test_recovery_precedes_human_gate_and_spares_unrelated_blocker(sandbox):
    task = _seed_live_ollama_pilot_state(
        sandbox, extra_decisions=[UNRELATED_HUMAN_DECISION])
    a = str(sandbox["agentic"])

    # exactly what _run_cycle_locked's recovery prelude does, in the same
    # order, so we can inspect state precisely between the two steps --
    # equivalent to (and exercised end-to-end by) the full cycle above.
    bootstrap_events = bootstrap_gate.recover_bootstrap_deadlock(a)
    resolved_decisions = decision_policy.auto_resolve_reversible_decisions(
        a, str(sandbox["repo"]))

    assert [e["task_id"] for e in bootstrap_events] == ["t1-init-repo"]
    assert {d["decision"] for d in resolved_decisions} == {
        TEST_FRAMEWORK_DECISION, CSS_APPROACH_DECISION,
        BROWSER_BASELINE_DECISION}

    # the human gate, evaluated AFTER recovery, sees only the genuinely
    # unresolved decision -- not the two things recovery just fixed.
    human = projstate.open_blockers(a, human_only=True)
    assert [b["reason"] for b in human] == [UNRELATED_HUMAN_DECISION]

    decisions = projstate.read_yaml(a, "decisions.yaml", {})
    assert decisions["human_decisions_needed"] == [UNRELATED_HUMAN_DECISION]

    # the recovered task is eligible again and the cycle proceeds to work
    # on it (invoking the backend) instead of bouncing straight back to
    # human_required -- ordering matters precisely because a stale
    # human-gate check run BEFORE recovery would have blocked this.
    caller = _std_caller(task)
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "success"
    assert [c["role"] for c in caller.calls if c["role"] == "coder"]

    # the unrelated decision/blocker is still exactly where it was --
    # untouched by either recovery step.
    decisions_after = projstate.read_yaml(a, "decisions.yaml", {})
    assert decisions_after["human_decisions_needed"] == [
        UNRELATED_HUMAN_DECISION]
    blockers_after = projstate.read_yaml(a, "blockers.yaml", {}).get(
        "blockers", [])
    unrelated = [b for b in blockers_after
                if b["reason"] == UNRELATED_HUMAN_DECISION]
    assert len(unrelated) == 1 and not unrelated[0]["resolved"]


# 6: the same auto-resolution behaves identically in cycle_review mode -------
def test_reversible_decisions_resolve_identically_in_cycle_review_mode(
        sandbox):
    task = _seed_live_ollama_pilot_state(sandbox, mode="cycle_review")
    a = str(sandbox["agentic"])
    caller = _std_caller(task)
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())

    assert result["status"] == "success"
    decisions = projstate.read_yaml(a, "decisions.yaml", {})
    assert decisions["human_decisions_needed"] == []
    assert len(decisions["decided"]) == 3
    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t1-init-repo"]["status"] == "done"
    # cycle_review's own per-cycle notification still fires normally --
    # auto-resolution did not corrupt or suppress it.
    log = (sandbox["agentic"] / "memory" / "notifications.log").read_text()
    assert "cycle_complete" in log


# blocker dedup at the projstate layer ----------------------------------------
def test_add_blocker_deduplicates_by_task_and_code(sandbox):
    a = str(sandbox["agentic"])
    seed_project(sandbox, [simple_task()])
    projstate.add_blocker(a, "t1-first", "no deterministic checks configured",
                          human_only=True,
                          code=projstate.BLOCKER_CODE_DETERMINISTIC_CHECKS_MISSING)
    projstate.add_blocker(a, "t1-first", "no deterministic checks configured",
                          human_only=False,
                          code=projstate.BLOCKER_CODE_DETERMINISTIC_CHECKS_MISSING)
    blockers = projstate.read_yaml(a, "blockers.yaml", {}).get("blockers", [])
    assert len(blockers) == 1
    assert blockers[0]["human_only"] is True   # the FIRST recorded wins

    # a different code for the same task is a distinct blocker
    projstate.add_blocker(a, "t1-first", "protected path in work order",
                          code=projstate.BLOCKER_CODE_POLICY_DENIED)
    blockers = projstate.read_yaml(a, "blockers.yaml", {}).get("blockers", [])
    assert len(blockers) == 2

    # calls without a code never dedup (unchanged legacy behaviour)
    projstate.add_blocker(a, None, "some human decision", human_only=True)
    projstate.add_blocker(a, None, "some human decision", human_only=True)
    blockers = projstate.read_yaml(a, "blockers.yaml", {}).get("blockers", [])
    assert len(blockers) == 4
