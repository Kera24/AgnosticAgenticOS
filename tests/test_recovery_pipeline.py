"""The `project recover <id>` pipeline (item 4): every stage returns a
structured result, even when nothing needs recovering. Covers failure-
streak reconciliation (item 6, platform failures excluded from the
streak/cooldown but genuine ones retained), dependency/worktree
reconciliation, idempotence, and the end-to-end proof that a task stuck
on the aggregate blocker never reaches the backend until recovery clears
it."""
import json
import os

from conftest import Clock, FakeCaller, project_cfg, proj_order, seed_project, simple_task, worker_out
from core import logs, parallel_recovery, projstate, recovery
from core.project import run_cycle
from core.scheduler import Scheduler

OLLAMA_PILOT_REASON = (
    "parallel candidates exhausted: all 2 candidate(s) disqualified "
    "(t1-init-repo: .gitignore is in allowed_paths but rejected by "
    "bootstrap-expected-paths validation; src/ and tests/ directory "
    "creation req")

ALL_STAGES = (
    "stale_owned_process_recovery", "stale_lease_recovery",
    "blocker_code_migration", "aggregate_candidate_cause_reconstruction",
    "fixed_platform_defect_recovery", "task_state_reconciliation",
    "dependency_reconciliation", "failure_streak_reconciliation",
    "worktree_compatibility_validation",
)


def _scheduler(sandbox, clock=None):
    memdir = str(sandbox["agentic"] / "memory")
    return Scheduler(sandbox["cfg"], memdir, clock=clock or Clock())


# -- item 4: every stage reports a structured result, even when idle ----------

def test_run_recovery_reports_every_stage_even_when_nothing_to_recover(
        sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    scheduler = _scheduler(sandbox)
    stages = recovery.run_recovery(sandbox["cfg"], a, str(sandbox["repo"]),
                                   scheduler, Clock(), log=lambda e: None)
    for name in ALL_STAGES:
        assert name in stages, name
        assert "count" in stages[name]


def test_recovery_pipeline_is_idempotent(sandbox):
    project_cfg(sandbox)
    task = simple_task("t1-init-repo", kind="bootstrap")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    projstate.update_task(a, "t1-init-repo", status="blocked",
                          blocking_reason=OLLAMA_PILOT_REASON)
    projstate.add_blocker(a, "t1-init-repo", OLLAMA_PILOT_REASON,
                          human_only=False, code=None)
    scheduler = _scheduler(sandbox)
    first = recovery.run_recovery(sandbox["cfg"], a, str(sandbox["repo"]),
                                  scheduler, Clock(), log=lambda e: None)
    assert first["aggregate_candidate_cause_reconstruction"]["count"] == 1
    second = recovery.run_recovery(sandbox["cfg"], a, str(sandbox["repo"]),
                                   scheduler, Clock(), log=lambda e: None)
    # nothing left to migrate the second time -- already resolved
    assert second["aggregate_candidate_cause_reconstruction"]["count"] == 0
    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t1-init-repo"]["status"] == "pending"


def test_recovery_clears_fixed_windows_codex_readonly_blocker(
        sandbox, monkeypatch):
    project_cfg(sandbox)
    sandbox["cfg"].setdefault("backends", {})["codex"] = {
        "type": "cli", "kind": "codex",
        "binary": "C:/complete/codex.exe",
        "ignore_user_config": False,
    }
    monkeypatch.setattr(recovery, "_is_native_windows", lambda: True)
    task = simple_task("t1-init-repo", kind="bootstrap")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    reason = ("Workspace is read-only, so the required scaffold files and "
              "directories cannot be created.")
    projstate.update_task(a, task["id"], status="blocked",
                          blocking_reason=reason, last_result="failure")
    projstate.add_blocker(a, task["id"], reason, code=None,
                          human_only=False)

    stages = recovery.run_recovery(
        sandbox["cfg"], a, str(sandbox["repo"]), _scheduler(sandbox),
        Clock(), log=lambda e: None)

    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks[task["id"]]["status"] == "pending"
    assert tasks[task["id"]]["blocking_reason"] is None
    assert projstate.open_blockers(a) == []
    events = stages["fixed_platform_defect_recovery"]["events"]
    assert any(e["task_id"] == task["id"] for e in events)


def test_windows_codex_readonly_signature_overrides_stale_false_classification(
        sandbox):
    project_cfg(sandbox)
    seed_project(sandbox, [simple_task()])
    a = str(sandbox["agentic"])
    memdir = str(sandbox["agentic"] / "memory")
    reason = ("Workspace is read-only, so the required scaffold files and "
              "directories cannot be created.")
    _write_cycle(memdir, "r1", "success", "ok")
    _write_cycle(memdir, "r2", "failure", reason,
                 failure_class="workspace_policy_denied", platform=False)
    scheduler = _scheduler(sandbox)
    scheduler.state["failure_streak"] = 1
    scheduler.save()

    report = recovery.reconstruct_failure_streak(a, scheduler)

    assert report["resulting_streak"] == 0
    assert [r["run_id"] for r in report["removed_platform_failures"]] == [
        "r2"]


def test_recovery_uses_gate_artifact_for_fixed_windows_npm_resolution(
        sandbox, monkeypatch):
    project_cfg(sandbox)
    task = simple_task("t1-init-repo", kind="bootstrap")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    reason = "repair attempts exhausted"
    projstate.update_task(a, task["id"], status="blocked",
                          blocking_reason=reason, last_result="failure")
    projstate.add_blocker(a, task["id"], reason, code=None,
                          human_only=False)

    run_dir = sandbox["agentic"] / "runs" / "cycle-r2"
    run_dir.mkdir(parents=True)
    (run_dir / "work-order.json").write_text(
        json.dumps({"item": task["id"]}), encoding="utf-8")
    (run_dir / "validation-result-3.json").write_text(json.dumps({
        "ok": False,
        "results": [{
            "name": "npm-test",
            "command": "npm run test --silent",
            "exit_code": 127,
            "detail": "command not found: npm",
        }],
    }), encoding="utf-8")

    memdir = str(sandbox["agentic"] / "memory")
    _write_cycle(memdir, "r1", "success", "ok")
    _write_cycle(memdir, "r2", "failure", reason,
                 failure_class="deterministic_check_failed", platform=False)
    scheduler = _scheduler(sandbox)
    scheduler.state["failure_streak"] = 1
    scheduler.save()
    monkeypatch.setattr(recovery, "_is_native_windows", lambda: True)
    monkeypatch.setattr(
        recovery, "_windows_command_available", lambda command: command == "npm")

    stages = recovery.run_recovery(
        sandbox["cfg"], a, str(sandbox["repo"]), scheduler, Clock(),
        log=lambda e: None)

    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks[task["id"]]["status"] == "pending"
    assert projstate.open_blockers(a) == []
    assert stages["failure_streak_reconciliation"]["resulting_streak"] == 0
    events = stages["fixed_platform_defect_recovery"]["events"]
    assert any(e["action"] ==
               "reset_windows_command_resolution_blocker" for e in events)


# -- item 6: failure-streak reconciliation --------------------------------------

def _write_cycle(memdir, run_id, outcome, detail, failure_class=None,
                 platform=None):
    if failure_class:
        logs.decision(memdir, {"event": "failure_classified",
                               "run_id": run_id, "task_id": "t1",
                               "failure_class": failure_class,
                               "platform_class": bool(platform)})
    logs.decision(memdir, {"event": "cycle_finished", "run_id": run_id,
                           "outcome": outcome, "detail": detail,
                           "task_id": "t1"})


def test_failure_streak_reconciliation_removes_structured_platform_failures(
        sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    memdir = str(sandbox["agentic"] / "memory")
    _write_cycle(memdir, "r1", "success", "ok")
    _write_cycle(memdir, "r2", "failure", "genuine model failure 1",
                failure_class="model_output_invalid", platform=False)
    _write_cycle(memdir, "r3", "failure", "platform contract bug",
                failure_class="task_contract_invalid", platform=True)
    _write_cycle(memdir, "r4", "failure", "genuine model failure 2",
                failure_class="model_output_invalid", platform=False)
    _write_cycle(memdir, "r5", "failure", "platform capability bug",
                failure_class="platform_capability_missing", platform=True)
    scheduler = _scheduler(sandbox)
    scheduler.state["failure_streak"] = 4
    scheduler.save()
    report = recovery.reconstruct_failure_streak(a, scheduler)
    assert report["previous_streak"] == 4
    assert report["resulting_streak"] == 2   # only the two genuine failures
    assert len(report["retained_genuine_failures"]) == 2
    assert len(report["removed_platform_failures"]) == 2
    assert scheduler.state["failure_streak"] == 2
    assert report["resulting_cooling"]["failure_streak"] == 2


def test_failure_streak_reconciliation_recognises_legacy_signature_without_event(
        sandbox):
    """A cycle predating `failure_classified` instrumentation (the exact
    bug this fix closes) is still retroactively recognised as
    platform-caused via the same legacy signature evidence used to
    migrate blockers."""
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    memdir = str(sandbox["agentic"] / "memory")
    _write_cycle(memdir, "r1", "success", "ok")
    _write_cycle(memdir, "r2", "failure",
                "all parallel candidates failed: all 2 candidate(s) "
                "disqualified (t1-init-repo: .gitignore is in "
                "allowed_paths but rejected by bootstrap-expected-paths "
                "validation)")   # no failure_classified event at all
    scheduler = _scheduler(sandbox)
    scheduler.state["failure_streak"] = 1
    scheduler.save()
    report = recovery.reconstruct_failure_streak(a, scheduler)
    assert report["resulting_streak"] == 0
    assert len(report["removed_platform_failures"]) == 1


def test_failure_streak_reconciliation_never_blindly_zeroes_genuine_failures(
        sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    memdir = str(sandbox["agentic"] / "memory")
    _write_cycle(memdir, "r1", "failure", "genuine failure only",
                failure_class="model_output_invalid", platform=False)
    scheduler = _scheduler(sandbox)
    scheduler.state["failure_streak"] = 1
    scheduler.save()
    report = recovery.reconstruct_failure_streak(a, scheduler)
    assert report["resulting_streak"] == 1
    assert report["removed_platform_failures"] == []


def test_failure_streak_reconciliation_rate_limit_never_counted(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    memdir = str(sandbox["agentic"] / "memory")
    _write_cycle(memdir, "r1", "success", "ok")
    _write_cycle(memdir, "r2", "rate_limit", "provider rate limited")
    _write_cycle(memdir, "r3", "failure", "genuine failure",
                failure_class="model_output_invalid", platform=False)
    scheduler = _scheduler(sandbox)
    report = recovery.reconstruct_failure_streak(a, scheduler)
    assert report["resulting_streak"] == 1


# -- dependency / worktree stages ------------------------------------------------

def test_dependency_reconciliation_reports_newly_eligible_tasks(sandbox):
    project_cfg(sandbox)
    t1 = simple_task("t1-a")
    t2 = simple_task("t2-b", dependencies=["t1-a"])
    seed_project(sandbox, [t1, t2])
    a = str(sandbox["agentic"])
    before = recovery._dependency_reconciliation(a)
    assert before == []   # t1-a not done yet
    projstate.update_task(a, "t1-a", status="done")
    after = recovery._dependency_reconciliation(a)
    assert any(e["task_id"] == "t2-b" for e in after)


def test_worktree_compatibility_validation_reports_preserved_changes(sandbox):
    project_cfg(sandbox)
    task = simple_task("t1-init-repo", kind="bootstrap")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    root = str(sandbox["repo"])
    from core import taskspace
    from core.project import PROJECT_BRANCH, ensure_project_worktree
    ensure_project_worktree(sandbox["cfg"], {
        "agentic": a, "root": root,
        "memory": str(sandbox["agentic"] / "memory"),
        "queue": str(sandbox["agentic"] / "queue"),
        "runs": str(sandbox["agentic"] / "runs")})
    path = taskspace.create_task_worktree(root, a, "t1-init-repo",
                                          PROJECT_BRANCH)
    with open(os.path.join(path, "leftover.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("leftover\n")
    from conftest import git
    git(["add", "-A"], path)
    report = recovery._worktree_compatibility_validation(a)
    assert any(r["task_id"] == "t1-init-repo" for r in report)


def test_task_state_reconciliation_resets_stuck_in_progress_task(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    projstate.update_task(a, "t1-first", status="in_progress")
    reconciled = recovery._task_state_reconciliation(a)
    assert reconciled and reconciled[0]["task_id"] == "t1-first"
    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t1-first"]["status"] == "pending"


# -- end-to-end: no backend call while blocked, dispatch resumes after recovery -

def qa_pass():
    return {"verdict": "pass", "done_when_results": [], "reason": "ok",
           "out_of_scope_changes": [], "test_integrity_preserved": True}


def sec_pass():
    return {"verdict": "pass", "concerns": [], "reason": "clean"}


def test_no_backend_invocation_while_blocked_then_recovery_unblocks_dispatch(
        sandbox):
    project_cfg(sandbox)
    task = simple_task("t1-first")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    projstate.update_task(a, "t1-first", status="blocked",
                          blocking_reason=OLLAMA_PILOT_REASON)
    projstate.add_blocker(a, "t1-first", OLLAMA_PILOT_REASON,
                          human_only=False, code=None)
    assert projstate.next_task(a) is None   # nothing dispatchable while blocked

    caller = FakeCaller({
        "conductor": proj_order(task), "coder": worker_out(),
        "qa": qa_pass(), "security": sec_pass()})
    # the per-cycle startup recovery (wired into project.py) self-heals
    # this exact legacy shape BEFORE task selection in the same cycle
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "success"
    coder_calls = [c for c in caller.calls if c["role"] == "coder"]
    assert len(coder_calls) == 1   # the backend WAS invoked this cycle
    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t1-first"]["status"] == "done"


# -- item 7: single-candidate override for the native proof --------------------

def test_set_override_forces_exactly_one_candidate_for_one_run(sandbox):
    """`--set parallelism.max_agents_global=1` (documented in
    `.agentic/run`'s Overrides section) must force exactly one candidate
    even for a task that would otherwise trigger Phase-5 fan-out, using
    the EXISTING config convention -- never a new, overlapping flag."""
    project_cfg(sandbox)
    sandbox["cfg"]["parallelism"] = {"max_agents_global": 1}
    task = simple_task(risk="high")   # would normally trigger 2 candidates
    seed_project(sandbox, [task])
    caller = FakeCaller({
        "conductor": proj_order(task), "coder": worker_out(),
        "qa": qa_pass(), "security": sec_pass()})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "success"
    coder_calls = [c for c in caller.calls if c["role"] == "coder"]
    assert len(coder_calls) == 1   # forced single-candidate, no fan-out


def test_set_override_is_in_memory_only_never_touches_config_files():
    """The override must affect one run only -- verified at the
    config-loading layer: `--set` never writes config.yaml/
    config.machine.yaml, it only mutates the returned in-memory dict."""
    from core.config import AGENTIC_DIR, load_config
    config_path = AGENTIC_DIR / "config.yaml"
    before = config_path.read_bytes()
    cfg = load_config(cli_overrides={"parallelism.max_agents_global": "1"})
    assert cfg["parallelism"]["max_agents_global"] == 1
    after = config_path.read_bytes()
    assert before == after   # never persisted to disk
