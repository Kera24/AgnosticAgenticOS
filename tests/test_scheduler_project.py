"""Scheduler cooldowns and persistence, overlap prevention, restart/resume,
dependency ordering, conditional security review, repair flows, handoff,
zero-check blocking, notifications, final audit, and the mocked end-to-end
complete-project build."""
import json
import os

from conftest import (Clock, FakeCaller, project_cfg, proj_order,
                      seed_project, simple_task, verifier_out, worker_out)
from core import projstate
from core.project import (final_audit, project_pause, project_resume,
                          project_start, project_status, run_cycle,
                          security_review_required)
from core.scheduler import Scheduler


def edit_ok(**over):
    return worker_out(**over)


def qa_pass():
    return verifier_out("pass")


def sec_pass():
    return {"verdict": "pass", "concerns": [], "reason": "clean"}


def std_caller(task, extra=None, qa=None, coder=None):
    scripted = {"conductor": proj_order(task),
                "coder": coder or edit_ok(),
                "qa": qa or qa_pass(),
                "security": sec_pass()}
    scripted.update(extra or {})
    return FakeCaller(scripted)


def cycle(sandbox, caller, clock, run_id=None):
    return run_cycle(sandbox["cfg"], caller=caller, clock=clock,
                     run_id=run_id)


# 25-28. cooldown policy ---------------------------------------------------------------
def test_cooldown_defaults_and_dynamic(base_cfg, tmp_path):
    clock = Clock()
    scheduler = Scheduler(project_cfg({"cfg": base_cfg}),
                          str(tmp_path / "mem"), clock=clock)
    assert scheduler.cooldown_minutes("success") == 30          # 25
    assert scheduler.cooldown_minutes("failure") == 30          # 26
    # 27. dynamic rate-limit cooldown from explicit hints, clamped to [5,360]
    assert scheduler.cooldown_minutes("rate_limit",
                                      retry_after_seconds=1200) == 20
    assert scheduler.cooldown_minutes("rate_limit",
                                      retry_after_seconds=60) == 5
    # 28. dynamic usage-limit cooldown; defaults sensibly without hints
    assert scheduler.cooldown_minutes("usage_limit",
                                      retry_after_seconds=100000) == 360
    assert scheduler.cooldown_minutes("usage_limit") == 60


# 29. persistent scheduler state ----------------------------------------------------------
def test_scheduler_state_persists_across_instances(base_cfg, tmp_path):
    clock = Clock()
    cfg = project_cfg({"cfg": base_cfg})
    s1 = Scheduler(cfg, str(tmp_path / "mem"), clock=clock)
    s1.begin_cycle("run-1", "mock")
    until = s1.start_cooling("success")
    # a brand-new process sees the same state
    s2 = Scheduler(cfg, str(tmp_path / "mem"), clock=clock)
    assert s2.state["state"] == "cooling"
    assert s2.state["current_cycle"] == "run-1"
    assert s2.state["selected_backend"] == "mock"
    ok, reason = s2.eligible()
    assert not ok and "cooling until" in reason
    clock.advance(minutes=31)
    assert s2.eligible()[0] is True
    assert until.isoformat(timespec="seconds") == s2.state["next_run_at"]


def test_operating_window(base_cfg, tmp_path):
    cfg = project_cfg({"cfg": base_cfg})
    cfg["scheduler"]["operating_window"] = {"enabled": True, "start": "07:00",
                                            "stop": "22:00"}
    clock = Clock()   # noon -> inside
    scheduler = Scheduler(cfg, str(tmp_path / "mem"), clock=clock)
    assert scheduler.eligible()[0] is True
    clock.advance(minutes=11 * 60)   # 23:00 -> outside
    ok, reason = scheduler.eligible()
    assert not ok and "operating window" in reason


# 30. prevention of overlapping cycles --------------------------------------------------------
def test_overlapping_cycles_prevented(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    lock = projstate.ProjectLock(str(sandbox["agentic"]))
    assert lock.acquire()
    try:
        result = cycle(sandbox, std_caller(task), Clock())
        assert result["status"] == "locked"
    finally:
        lock.release()


# full pass + 42. preservation of unrelated user changes ------------------------------------------
def test_successful_cycle_commits_and_preserves_user_tree(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    dirty = sandbox["repo"] / "tests_placeholder.txt"
    dirty.write_text("user work in progress\n", encoding="utf-8")

    result = cycle(sandbox, std_caller(task), Clock(), run_id="c1")
    assert result["status"] == "success"
    # backlog persisted as done; cooling scheduled
    tasks = projstate.load_backlog(str(sandbox["agentic"]))
    assert tasks[0]["status"] == "done" and tasks[0]["last_result"] == "pass"
    assert result["cooling_until"]
    # committed on the project branch, not in the user's tree
    import subprocess
    show = subprocess.run(["git", "show", "agentic/project:src/app.py"],
                          cwd=sandbox["repo"], capture_output=True, text=True)
    assert show.stdout == "VALUE = 2\n"
    assert (sandbox["repo"] / "src" / "app.py").read_text() == "VALUE = 1\n"
    assert dirty.read_text() == "user work in progress\n"


# 31. project restart and resume ------------------------------------------------------------------
def test_restart_resume_continues_without_regenerating(sandbox):
    project_cfg(sandbox)
    t1, t2 = simple_task("t1-first"), simple_task(
        "t2-second", dependencies=["t1-first"],
        description="set VALUE to 3")
    seed_project(sandbox, [t1, t2])
    clock = Clock()
    assert cycle(sandbox, std_caller(t1), clock,
                 run_id="c1")["status"] == "success"
    # simulate cooling block, then a fresh process after restart
    result = cycle(sandbox, std_caller(t2), clock, run_id="c2")
    assert result["status"] == "not_eligible"
    clock.advance(minutes=31)
    caller2 = std_caller(t2, coder=edit_ok(edits=[
        {"path": "src/app.py", "action": "write", "content": "VALUE = 3\n"}]))
    result = cycle(sandbox, caller2, clock, run_id="c2")
    assert result["status"] == "success"
    # t1 was NOT regenerated: conductor saw only t2
    assert [c["input"]["task"]["id"] for c in caller2.calls
            if c["role"] == "conductor"] == ["t2-second"]
    statuses = {t["id"]: t["status"]
                for t in projstate.load_backlog(str(sandbox["agentic"]))}
    assert statuses == {"t1-first": "done", "t2-second": "done"}


def test_pause_and_resume(sandbox):
    project_cfg(sandbox)
    seed_project(sandbox, [simple_task()])
    project_pause(sandbox["cfg"])
    result = cycle(sandbox, std_caller(simple_task()), Clock())
    assert result["status"] == "not_eligible" and "paused" in result["reason"]
    project_resume(sandbox["cfg"])
    status = project_status(sandbox["cfg"])
    assert status["scheduler"]["state"] == "idle"


# 32/33. milestone and task dependency ordering ------------------------------------------------------
def test_dependency_and_milestone_ordering(sandbox):
    project_cfg(sandbox)
    tasks = [
        simple_task("a-early-id-late-milestone", milestone="m2"),
        simple_task("z-first-milestone", milestone="m1"),
        simple_task("z-second", milestone="m1",
                    dependencies=["z-first-milestone"]),
    ]
    seed_project(sandbox, tasks,
                 milestones=[{"id": "m1", "title": "one"},
                             {"id": "m2", "title": "two"}])
    a = str(sandbox["agentic"])
    assert projstate.next_task(a)["id"] == "z-first-milestone"
    projstate.update_task(a, "z-first-milestone", status="done")
    assert projstate.next_task(a)["id"] == "z-second"       # dep now satisfied
    projstate.update_task(a, "z-second", status="done")
    assert projstate.next_task(a)["id"] == "a-early-id-late-milestone"


# 34. conditional security review ---------------------------------------------------------------------
def test_security_review_runs_conditionally(sandbox):
    project_cfg(sandbox)
    benign = simple_task()
    seed_project(sandbox, [benign])
    caller = std_caller(benign)
    assert cycle(sandbox, caller, Clock())["status"] == "success"
    assert not [c for c in caller.calls if c["role"] == "security"]

    assert security_review_required({"security_relevant": True}, [], "")
    assert security_review_required({}, ["src/auth/login.py"], "")
    assert security_review_required({}, [], "+ password = input()")
    assert not security_review_required({}, ["docs/readme.md"], "+ x = 1")


def test_security_relevant_task_reviewed_and_can_block(sandbox):
    project_cfg(sandbox)
    task = simple_task(security_relevant=True)
    seed_project(sandbox, [task])
    caller = std_caller(task)
    caller.by_role["security"] = [{"verdict": "fail",
                                   "concerns": [{"severity": "high",
                                                 "description": "sqli"}],
                                   "reason": "injection risk"}]
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "failure"
    assert [c for c in caller.calls if c["role"] == "security"]
    tasks = projstate.load_backlog(str(sandbox["agentic"]))
    assert tasks[0]["status"] == "blocked"
    assert "security" in tasks[0]["blocking_reason"]


# 35. QA failure repair -------------------------------------------------------------------------------------
def test_qa_failure_triggers_repair_then_pass(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    caller = std_caller(task, qa=[verifier_out("fail"), qa_pass(), qa_pass()])
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "success"
    coder_calls = [c for c in caller.calls if c["role"] == "coder"]
    assert len(coder_calls) == 2
    assert "qa_findings" in (coder_calls[1]["input"] or {})


# 36. repair attempt limit --------------------------------------------------------------------------------------
def test_repair_attempts_exhausted_blocks_task(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = [
        {"name": "always-fails",
         "command": ["python", "-c", "import sys; sys.exit(1)"],
         "mandatory": True}]
    from core import gate
    gate.save_baseline(str(sandbox["agentic"]),
                       [{"name": "always-fails", "passed": True}])
    task = simple_task()
    seed_project(sandbox, [task])
    # each repair attempt produces a DIFFERENT diff (otherwise the identical
    # failure fingerprint short-circuits the loop — tested separately)
    caller = std_caller(task, coder=[
        edit_ok(),
        edit_ok(edits=[{"path": "src/app.py", "action": "write",
                        "content": "VALUE = 3\n"}]),
        edit_ok(edits=[{"path": "src/app.py", "action": "write",
                        "content": "VALUE = 4\n"}])])
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "failure"
    assert len([c for c in caller.calls if c["role"] == "coder"]) == 3
    tasks = projstate.load_backlog(str(sandbox["agentic"]))
    assert tasks[0]["status"] == "blocked"
    assert "repair attempts exhausted" in tasks[0]["blocking_reason"]
    # repair inputs carried the failing checks forward
    second = [c for c in caller.calls if c["role"] == "coder"][1]
    assert second["input"]["failing_checks"][0]["name"] == "always-fails"


# 37. structured backend handoff -----------------------------------------------------------------------------------
def test_structured_handoff_on_mid_task_exhaustion(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    caller = std_caller(task, coder=[
        {"_error": "usage_limit", "backend": "mock", "retry_after": 3600},
        edit_ok(), edit_ok()])
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "success"
    coder_calls = [c for c in caller.calls if c["role"] == "coder"]
    assert len(coder_calls) == 2
    handoff_input = coder_calls[1]["input"]
    assert handoff_input["handoff"] is True
    assert handoff_input["original_work_order"]["skill"] == task["skill"]
    assert "allowed_paths" in handoff_input
    assert "remaining_criteria" in handoff_input
    assert coder_calls[1]["chain"] == ["mock2"]   # rerouted to the fallback


# 38. zero deterministic checks blocking completion ------------------------------------------------------------------
def test_zero_checks_block_cycle_and_queue_configuration(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    task = simple_task()
    seed_project(sandbox, [task])
    result = cycle(sandbox, std_caller(task), Clock())
    assert result["status"] == "failure"
    assert "zero deterministic checks" in result["detail"]
    tasks = projstate.load_backlog(str(sandbox["agentic"]))
    assert tasks[0]["status"] == "blocked"
    blockers = projstate.open_blockers(str(sandbox["agentic"]),
                                       human_only=True)
    assert any("deterministic checks" in b["reason"] for b in blockers)


# 41. protected-path rejection in project mode --------------------------------------------------------------------------
def test_protected_path_edit_rejected_then_repaired(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    caller = std_caller(task, coder=[
        edit_ok(edits=[{"path": ".env", "action": "write",
                        "content": "X=1\n"},
                       {"path": "src/app.py", "action": "write",
                        "content": "VALUE = 2\n"}]),
        edit_ok(), edit_ok()])
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "success"          # repaired within limits
    coder_calls = [c for c in caller.calls if c["role"] == "coder"]
    assert len(coder_calls) == 2
    assert "scope_violations" in coder_calls[1]["input"]
    worktree = sandbox["agentic"] / "worktrees" / "project"
    assert not (worktree / ".env").exists()
    assert not (sandbox["repo"] / ".env").exists()


def test_work_order_granting_protected_paths_blocks(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    caller = std_caller(task)
    caller.by_role["conductor"] = [proj_order(task,
                                              allowed_paths=[".env",
                                                             "src/**"])]
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "failure"
    assert "protected path" in projstate.load_backlog(
        str(sandbox["agentic"]))[0]["blocking_reason"]


# 43/44. notification behaviour --------------------------------------------------------------------------------------------
def test_completion_only_suppresses_cycle_notifications(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    cycle(sandbox, std_caller(task), Clock())
    log = sandbox["agentic"] / "memory" / "notifications.log"
    assert not log.exists() or "cycle_complete" not in log.read_text()


def test_cycle_review_mode_notifies_each_cycle(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["interaction"]["mode"] = "cycle_review"
    task = simple_task()
    seed_project(sandbox, [task])
    cycle(sandbox, std_caller(task), Clock())
    log = sandbox["agentic"] / "memory" / "notifications.log"
    assert "cycle_complete" in log.read_text()


def test_human_only_blocker_notifies(sandbox):
    project_cfg(sandbox)
    seed_project(sandbox, [simple_task(status="blocked")])
    projstate.add_blocker(str(sandbox["agentic"]), None,
                          "choose a payment provider account",
                          human_only=True)
    result = cycle(sandbox, FakeCaller({}), Clock())
    assert result["status"] == "human_required"
    log = sandbox["agentic"] / "memory" / "notifications.log"
    assert "human_blocker" in log.read_text()
    assert "payment provider" in log.read_text()


# 46. secret scanning ----------------------------------------------------------------------------------------------------------
def test_secret_in_edit_is_rejected(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    caller = std_caller(task, coder=edit_ok(edits=[
        {"path": "src/app.py", "action": "write",
         "content": "KEY = 'sk-abcdefghijklmnop1234567890'\n"}]))
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "failure"
    worktree = sandbox["agentic"] / "worktrees" / "project"
    assert "sk-" not in (worktree / "src" / "app.py").read_text()


# 47. no automatic push, remote merge or deployment ------------------------------------------------------------------------------
def test_no_push_merge_or_deploy_anywhere_in_core():
    """Push/pull/deploy are banned everywhere. A LOCAL merge into the
    project's agentic branch is the sanctioned integration mechanism and
    exists ONLY in taskspace.integrate_task (MP Phase 3)."""
    import pathlib
    from conftest import AGENTIC_SRC
    banned = ["\"push\"", "'push'", "\"pull\"", "'pull'", "\"deploy\""]
    merge_allowed = {"taskspace.py"}
    # supabasex.py's "push" is `supabase db push` — the policy-gated,
    # dry-run-first, approval-guarded migration apply (MP Phase 7); it is
    # not a git/deploy operation.
    push_allowed = {"supabasex.py"}
    offenders = []
    for path in list(pathlib.Path(str(AGENTIC_SRC / "core")).rglob("*.py")) \
            + list(pathlib.Path(str(AGENTIC_SRC / "providers")).rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in banned:
            if token in text:
                if "push" in token and path.name in push_allowed:
                    continue
                offenders.append("%s: %s" % (path.name, token))
        if ("\"merge\"" in text or "'merge'" in text) and \
                path.name not in merge_allowed:
            offenders.append("%s: merge outside taskspace" % path.name)
    assert offenders == []


# 45 + 48. final audit and the mocked end-to-end complete-project build -------------------------------------------------------------
ARCHITECT_OUT = {
    "architecture": "single-module python app",
    "assumptions": ["no external services"],
    "milestones": [{"id": "m1", "title": "core"}],
    "backlog": [
        {"id": "t1-value", "milestone": "m1",
         "description": "set VALUE to 2", "dependencies": [], "risk": "low",
         "security_relevant": False, "expected_paths": ["src/**"],
         "expected_size": "small",
         "acceptance_criteria": ["VALUE equals 2"],
         "deterministic_checks": [], "skill": "app-code"},
        {"id": "t2-greeting", "milestone": "m1",
         "description": "add greeting()", "dependencies": ["t1-value"],
         "risk": "low", "security_relevant": False,
         "expected_paths": ["src/**"], "expected_size": "small",
         "acceptance_criteria": ["greeting() returns hello"],
         "deterministic_checks": [], "skill": "app-code"},
    ],
    "requirements_map": [{"requirement": "value is 2", "tasks": ["t1-value"]}],
    "completion_criteria": ["all mandatory checks pass"],
    "human_decisions": [],
}


def test_mocked_end_to_end_complete_project_build(sandbox):
    project_cfg(sandbox)
    cfg = sandbox["cfg"]
    clock = Clock()
    plan = sandbox["repo"] / "plan.md"
    plan.write_text("# Build a tiny app\nVALUE must be 2; add greeting().",
                    encoding="utf-8")

    # 1. plan -> architect -> persistent project state
    start = project_start(cfg, str(plan),
                          caller=FakeCaller({"architect": ARCHITECT_OUT}),
                          clock=clock)
    assert start["status"] == "started" and start["tasks"] == 2
    a = str(sandbox["agentic"])
    assert projstate.exists(a)
    assert (sandbox["agentic"] / "project" / "PROJECT.md").exists()
    assert (sandbox["agentic"] / "project" / "architecture.md").exists()
    # idempotent: a second start never regenerates
    assert project_start(cfg, str(plan), caller=FakeCaller({}),
                         clock=clock)["status"] == "already_started"

    # 2. cycle 1 builds t1
    t1 = projstate.next_task(a)
    assert t1["id"] == "t1-value"
    r1 = cycle(sandbox, std_caller(t1), clock, run_id="e2e-1")
    assert r1["status"] == "success"

    # 3. cooling, then cycle 2 builds t2
    clock.advance(minutes=31)
    t2 = projstate.next_task(a)
    assert t2["id"] == "t2-greeting"
    caller2 = std_caller(t2, coder=edit_ok(edits=[
        {"path": "src/app.py", "action": "write",
         "content": "VALUE = 2\n\n\ndef greeting():\n    return 'hello'\n"}]))
    r2 = cycle(sandbox, caller2, clock, run_id="e2e-2")
    assert r2["status"] == "success"
    assert r2["progress"]["backlog_complete"] is True

    # 4. backlog empty -> next cycle runs the final audit -> complete
    clock.advance(minutes=31)
    r3 = run_cycle(cfg, caller=FakeCaller({"qa": qa_pass()}), clock=clock,
                   run_id="e2e-3")
    assert r3["status"] == "complete"
    audit = projstate.read_yaml(a, "final-audit.yaml")
    assert audit["complete"] is True
    assert audit["checks"]["deterministic_checks_pass"] is True
    assert audit["checks"]["no_committed_secrets"] is True
    assert audit["checks"]["final_independent_review"] is True

    # completion notification fired; scheduler marked complete
    log = (sandbox["agentic"] / "memory" / "notifications.log").read_text()
    assert "project_complete" in log
    scheduler = Scheduler(cfg, str(sandbox["agentic"] / "memory"))
    assert scheduler.state["state"] == "complete"
    # and a completed project never restarts on its own
    r4 = run_cycle(cfg, caller=FakeCaller({}), clock=clock)
    assert r4["status"] == "not_eligible"


def test_final_audit_fails_without_evidence(sandbox):
    project_cfg(sandbox)
    seed_project(sandbox, [simple_task(status="done"),
                           simple_task("t2-x", status="pending")])
    result = final_audit(sandbox["cfg"], caller=FakeCaller({"qa": qa_pass()}),
                         clock=Clock())
    assert result["status"] == "audit_failed"
    assert "backlog_complete" in result["failed_checks"]
