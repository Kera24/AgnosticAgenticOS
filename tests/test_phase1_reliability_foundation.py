"""Phase 1 -- Reliability Foundation: canonical task contract, capability
inventory, feasibility preflight, safe filesystem capability layer,
failure taxonomy, cooling transparency, and recovery migrations."""
import os

import pytest

from conftest import Clock, FakeCaller, git, project_cfg, proj_order, seed_project, simple_task, worker_out
from core import (contract as contract_mod, errors, failures, fscap,
                  inventory as inventory_mod, preflight as preflight_mod,
                  projstate)
from core.project import project_start, run_cycle


def qa_pass():
    return {"verdict": "pass", "done_when_results": [], "reason": "ok",
            "out_of_scope_changes": [], "test_integrity_preserved": True}


def std_caller(task, coder=None):
    return FakeCaller({
        "conductor": proj_order(task), "coder": coder or worker_out(),
        "qa": qa_pass(),
        "security": {"verdict": "pass", "concerns": [], "reason": "clean"},
    })


def cycle(sandbox, caller, clock, run_id=None):
    return run_cycle(sandbox["cfg"], caller=caller, clock=clock,
                     run_id=run_id)


# -- A. canonical task contract -----------------------------------------------

def test_build_task_contract_projects_task_and_order():
    task = simple_task(kind="bootstrap",
                       expected_paths=[{"path": "src/index.js",
                                       "type": "file", "required": True}])
    order = proj_order(task, allowed_paths=["src/**"])
    c = contract_mod.build_task_contract(task, order, "proj-1", run_id="r1")
    assert c["contract_version"] == contract_mod.CONTRACT_VERSION
    assert c["project_id"] == "proj-1"
    assert c["task_id"] == task["id"]
    assert c["kind"] == "bootstrap"
    assert c["allowed_paths"] == ["src/**"]
    assert c["required_outputs"][0]["path"] == "src/index.js"
    assert c["required_outputs"][0]["type"] == "file"
    assert not contract_mod.validate_contract_shape(c)


def test_contract_validate_shape_catches_missing_fields():
    problems = contract_mod.validate_contract_shape({"contract_version": "1.0"})
    assert any("task_id" in p for p in problems)


def test_contract_validate_shape_catches_bad_version():
    task = simple_task()
    order = proj_order(task)
    c = contract_mod.build_task_contract(task, order, "p")
    c["contract_version"] = "99.0"
    problems = contract_mod.validate_contract_shape(c)
    assert any("contract_version" in p for p in problems)


def test_contract_backward_compatible_with_legacy_task_shape():
    """A task with no `kind`, no dict-form expected_paths (a project
    started before this fix) still produces a valid contract -- no
    migration of backlog.yaml required."""
    task = {"id": "t1", "milestone": "m1", "description": "legacy task",
           "dependencies": [], "expected_paths": ["src/**"],
           "acceptance_criteria": []}
    order = {"item": "legacy", "allowed_paths": ["src/**"],
            "forbidden_paths": [], "maximum_changed_lines": 100,
            "risk": "low"}
    c = contract_mod.build_task_contract(task, order, "p")
    assert not contract_mod.validate_contract_shape(c)
    assert c["required_outputs"][0]["type"] == "glob"


# -- B. capability inventory --------------------------------------------------

def test_build_inventory_detects_python_and_git(tmp_path):
    git(["init", "-b", "main"], tmp_path)
    git(["config", "user.email", "t@t"], tmp_path)
    git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "app.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    git(["add", "-A"], tmp_path)
    git(["commit", "-m", "init"], tmp_path)
    inv = inventory_mod.build_inventory({}, str(tmp_path), str(tmp_path))
    assert inv["inventory_version"] == inventory_mod.INVENTORY_VERSION
    assert inv["observed"]["languages"].get("python") == 1
    assert inv["observed"]["git"]["branch"] == "main"
    assert "requirements.txt" in inv["observed"]["manifests_present"]
    assert inv["repository_revision"]


def test_inventory_persists_and_is_not_stale_immediately_after(tmp_path):
    git(["init", "-b", "main"], tmp_path)
    git(["config", "user.email", "t@t"], tmp_path)
    git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    git(["add", "-A"], tmp_path)
    git(["commit", "-m", "c"], tmp_path)
    agentic = tmp_path / ".agentic_state"
    inventory_mod.ensure_inventory({}, str(tmp_path), str(agentic))
    assert inventory_mod.is_stale(str(agentic), str(tmp_path)) is False
    loaded = inventory_mod.load(str(agentic))
    assert loaded["repository_revision"]


def test_inventory_stale_after_new_commit(tmp_path):
    git(["init", "-b", "main"], tmp_path)
    git(["config", "user.email", "t@t"], tmp_path)
    git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    git(["add", "-A"], tmp_path)
    git(["commit", "-m", "c1"], tmp_path)
    agentic = tmp_path / ".agentic_state"
    inventory_mod.ensure_inventory({}, str(tmp_path), str(agentic))
    (tmp_path / "f2.txt").write_text("y", encoding="utf-8")
    git(["add", "-A"], tmp_path)
    git(["commit", "-m", "c2"], tmp_path)
    assert inventory_mod.is_stale(str(agentic), str(tmp_path)) is True


def test_inventory_stale_after_manifest_content_change(tmp_path):
    git(["init", "-b", "main"], tmp_path)
    git(["config", "user.email", "t@t"], tmp_path)
    git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    git(["add", "-A"], tmp_path)
    git(["commit", "-m", "c1"], tmp_path)
    agentic = tmp_path / ".agentic_state"
    inventory_mod.ensure_inventory({}, str(tmp_path), str(agentic))
    # amend the manifest WITHOUT a new commit (uncommitted change) --
    # HEAD is unchanged but the manifest fingerprint must still catch it
    (tmp_path / "requirements.txt").write_text("pytest\nrequests\n",
                                                encoding="utf-8")
    assert inventory_mod.is_stale(str(agentic), str(tmp_path)) is True


def test_ensure_inventory_reuses_persisted_copy_when_fresh(tmp_path):
    git(["init", "-b", "main"], tmp_path)
    git(["config", "user.email", "t@t"], tmp_path)
    git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    git(["add", "-A"], tmp_path)
    git(["commit", "-m", "c"], tmp_path)
    agentic = tmp_path / ".agentic_state"
    first = inventory_mod.ensure_inventory({}, str(tmp_path), str(agentic))
    second = inventory_mod.ensure_inventory({}, str(tmp_path), str(agentic))
    assert first["observed_at"] == second["observed_at"]   # not rebuilt


# -- C. feasibility preflight --------------------------------------------------

def test_preflight_feasible_for_coverable_contract(tmp_path):
    task = simple_task(kind="bootstrap",
                       expected_paths=[{"path": "src/index.js",
                                       "type": "file", "required": True}])
    order = proj_order(task, allowed_paths=["src/**"])
    c = contract_mod.build_task_contract(task, order, "p")
    result = preflight_mod.run_preflight(c, task, [task], str(tmp_path),
                                         str(tmp_path))
    assert result["result"] == preflight_mod.RESULT_FEASIBLE
    assert result["consumes_capacity"] is True


def test_preflight_platform_invalid_for_impossible_output(tmp_path):
    task = simple_task(expected_paths=[{"path": "src/index.js",
                                        "type": "file", "required": True}])
    order = proj_order(task, allowed_paths=["docs/**"])
    c = contract_mod.build_task_contract(task, order, "p")
    result = preflight_mod.run_preflight(c, task, [task], str(tmp_path),
                                         str(tmp_path))
    assert result["result"] == preflight_mod.RESULT_PLATFORM_INVALID
    assert result["consumes_capacity"] is False


def test_preflight_dependency_wait_for_incomplete_dependency(tmp_path):
    task = simple_task(dependencies=["t0-earlier"])
    order = proj_order(task)
    c = contract_mod.build_task_contract(task, order, "p")
    backlog = [task, simple_task("t0-earlier")]
    result = preflight_mod.run_preflight(c, task, backlog, str(tmp_path),
                                         str(tmp_path))
    assert result["result"] == preflight_mod.RESULT_DEPENDENCY_WAIT
    assert result["consumes_capacity"] is False


def test_preflight_human_required_from_capacity_decision(tmp_path):
    task = simple_task()
    order = proj_order(task)
    c = contract_mod.build_task_contract(task, order, "p")
    result = preflight_mod.run_preflight(
        c, task, [task], str(tmp_path), str(tmp_path),
        capacity_decision={"decision": "human_required",
                           "reason": "no backend usable"})
    assert result["result"] == preflight_mod.RESULT_HUMAN_REQUIRED
    assert result["consumes_capacity"] is False


def test_preflight_credential_required_from_inventory(tmp_path):
    task = simple_task()
    order = proj_order(task)
    c = contract_mod.build_task_contract(task, order, "p")
    inventory = {"observed": {"cli_backend_verification": {
        "codex": {"ok": False, "detail": "not logged in"}}}}
    result = preflight_mod.run_preflight(
        c, task, [task], str(tmp_path), str(tmp_path), backend="codex",
        inventory=inventory)
    assert result["result"] == preflight_mod.RESULT_CREDENTIAL_REQUIRED


def test_preflight_unrelated_project_level_decision_never_blocks_task(
        tmp_path):
    """A pending human decision unrelated to this task must never
    preflight-block it (that would contradict the platform's own
    autonomous-progress design)."""
    task = simple_task(kind="bootstrap",
                       expected_paths=[{"path": "src/index.js",
                                       "type": "file", "required": True}])
    order = proj_order(task, allowed_paths=["src/**"])
    c = contract_mod.build_task_contract(task, order, "p")
    result = preflight_mod.run_preflight(
        c, task, [task], str(tmp_path), str(tmp_path),
        decisions_needed=["obtain payment provider API credentials"])
    assert result["result"] == preflight_mod.RESULT_FEASIBLE


def test_preflight_auto_repaired_for_unresolved_reversible_decision(
        tmp_path):
    task = simple_task(kind="bootstrap",
                       expected_paths=[{"path": "src/index.js",
                                       "type": "file", "required": True}])
    order = proj_order(task, allowed_paths=["src/**"])
    c = contract_mod.build_task_contract(task, order, "p")
    result = preflight_mod.run_preflight(
        c, task, [task], str(tmp_path), str(tmp_path),
        decisions_needed=["Choice of test framework (Jest/Vitest/Mocha)"])
    assert result["result"] == preflight_mod.RESULT_AUTO_REPAIRED


# -- D. safe filesystem capability layer --------------------------------------

def test_fscap_create_directory_and_file(tmp_path):
    worktree = str(tmp_path)
    fscap.create_directory(worktree, "src", ["src/**"], [])
    fscap.create_file(worktree, "src/index.js", "console.log(1)",
                      ["src/**"], [], [])
    assert os.path.isfile(os.path.join(worktree, "src", "index.js"))


def test_fscap_create_file_refuses_overwrite_when_disallowed(tmp_path):
    worktree = str(tmp_path)
    fscap.create_file(worktree, "a.txt", "one", ["**"], [], [])
    with pytest.raises(errors.PolicyError):
        fscap.create_file(worktree, "a.txt", "two", ["**"], [], [],
                          overwrite=False)


def test_fscap_read_and_list(tmp_path):
    worktree = str(tmp_path)
    fscap.create_file(worktree, "src/a.txt", "hi", ["**"], [], [])
    assert fscap.read_file(worktree, "src/a.txt") == "hi"
    assert "a.txt" in fscap.list_directory(worktree, "src")


def test_fscap_modify_file(tmp_path):
    worktree = str(tmp_path)
    fscap.create_file(worktree, "a.txt", "one", ["**"], [], [])
    fscap.modify_file(worktree, "a.txt", "two", ["**"], [], [])
    assert fscap.read_file(worktree, "a.txt") == "two"


def test_fscap_rename_path(tmp_path):
    worktree = str(tmp_path)
    fscap.create_file(worktree, "old.txt", "x", ["**"], [], [])
    fscap.rename_path(worktree, "old.txt", "new.txt", ["**"], [], [])
    assert not os.path.exists(os.path.join(worktree, "old.txt"))
    assert os.path.isfile(os.path.join(worktree, "new.txt"))


def test_fscap_rename_path_blocked_outside_allowed(tmp_path):
    worktree = str(tmp_path)
    fscap.create_file(worktree, "src/old.txt", "x", ["src/**"], [], [])
    with pytest.raises(errors.PolicyError):
        fscap.rename_path(worktree, "src/old.txt", "outside/new.txt",
                          ["src/**"], [], [])


def test_fscap_create_directory_blocks_traversal(tmp_path):
    with pytest.raises(errors.PolicyError):
        fscap.create_directory(str(tmp_path), "../escape", ["**"], [])


def test_fscap_inspect_git_read_only_allows_status(tmp_path):
    git(["init", "-b", "main"], tmp_path)
    out = fscap.inspect_git_read_only(str(tmp_path), ["status", "--porcelain"])
    assert out is not None


def test_fscap_inspect_git_read_only_rejects_write_commands(tmp_path):
    git(["init", "-b", "main"], tmp_path)
    with pytest.raises(errors.PolicyError):
        fscap.inspect_git_read_only(str(tmp_path), ["commit", "-m", "x"])
    with pytest.raises(errors.PolicyError):
        fscap.inspect_git_read_only(str(tmp_path), ["push"])


def test_fscap_run_approved_check_only_runs_allowlisted(tmp_path):
    allow = ["python -c \"import sys; sys.exit(0)\""]
    result = fscap.run_approved_check(
        "python -c \"import sys; sys.exit(0)\"", str(tmp_path), allow, 30)
    assert result["exit_code"] == 0
    skipped = fscap.run_approved_check("rm -rf /", str(tmp_path), allow, 30)
    assert skipped is None


def test_fscap_audit_events_emitted(tmp_path):
    events = []
    worktree = str(tmp_path)
    fscap.create_directory(worktree, "src", ["src/**"], [],
                           log=events.append)
    fscap.create_file(worktree, "src/a.txt", "x", ["src/**"], [], [],
                      log=events.append)
    names = [e["event"] for e in events]
    assert "fscap_create_directory" in names
    assert "fscap_create_file" in names
    assert all(e["layer"] == "fscap" for e in events)


# -- E. failure taxonomy --------------------------------------------------------

def test_failure_taxonomy_covers_all_twelve_classes():
    expected = {
        failures.MODEL_OUTPUT_INVALID, failures.PROVIDER_UNAVAILABLE,
        failures.PROVIDER_AUTHENTICATION, failures.PROVIDER_CAPACITY,
        failures.DETERMINISTIC_CHECK_FAILED,
        failures.PROJECT_DEPENDENCY_MISSING, failures.TASK_CONTRACT_INVALID,
        failures.PLATFORM_CAPABILITY_MISSING,
        failures.WORKSPACE_POLICY_DENIED, failures.GENUINE_HUMAN_DECISION,
        failures.EXECUTION_TIMEOUT, failures.INFRASTRUCTURE_FAILURE,
    }
    assert set(failures.TAXONOMY.keys()) == expected
    assert len(expected) == 12
    for cls, policy in failures.TAXONOMY.items():
        for key in ("retry_policy", "repair_policy", "fallback_eligible",
                   "cooling_behaviour", "blocker_behaviour",
                   "human_escalation", "persist_evidence"):
            assert key in policy, "%s missing %s" % (cls, key)


def test_platform_classes_never_fallback_eligible():
    """Platform-contract/capability failures must not trigger provider
    fallback -- the problem isn't the model, so trying a different model
    can't fix it."""
    for cls in failures.PLATFORM_CLASSES:
        assert failures.TAXONOMY[cls]["fallback_eligible"] is False


def test_cooling_outcome_for_platform_classes_is_platform_failure():
    for cls in failures.PLATFORM_CLASSES:
        assert failures.cooling_outcome_for(cls, "failure") == \
            "platform_failure"


def test_cooling_outcome_for_model_failure_unchanged():
    assert failures.cooling_outcome_for(
        failures.MODEL_OUTPUT_INVALID, "failure") is None
    assert failures.cooling_outcome_for(None, "failure") is None


# -- F. cooling clarity ----------------------------------------------------------

def test_platform_failure_cooling_is_short_and_flat(base_cfg, tmp_path):
    from core.scheduler import Scheduler
    scheduler = Scheduler(project_cfg({"cfg": base_cfg}), str(tmp_path / "m"))
    b1 = scheduler.cooldown_breakdown("platform_failure", failure_streak=1)
    b5 = scheduler.cooldown_breakdown("platform_failure", failure_streak=5)
    assert b1["clamped_minutes"] == b5["clamped_minutes"]   # never escalates
    assert b1["backoff_multiplier"] == 1.0
    assert b1["source"] == "configured_platform_failure_cooldown"
    assert b1["clamped_minutes"] < b1["configured_after_failure_minutes"]


def test_platform_failure_does_not_increment_failure_streak(base_cfg,
                                                             tmp_path):
    from core.scheduler import Scheduler
    scheduler = Scheduler(project_cfg({"cfg": base_cfg}), str(tmp_path / "m"))
    scheduler.start_cooling("platform_failure")
    scheduler.start_cooling("platform_failure")
    scheduler.start_cooling("platform_failure")
    assert scheduler.state["failure_streak"] == 0


def test_impossible_contract_cycle_uses_platform_failure_cooling(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    task = simple_task(expected_paths=[{"path": "src/index.js",
                                        "type": "file", "required": True}])
    seed_project(sandbox, [task])
    caller = std_caller(task)
    caller.by_role["conductor"] = [proj_order(task,
                                              allowed_paths=["docs/**"])]
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "failure"
    assert result["cooling_detail"]["source"] == \
        "configured_platform_failure_cooldown"
    from core.scheduler import Scheduler
    scheduler = Scheduler(sandbox["cfg"], str(sandbox["agentic"] / "memory"))
    assert scheduler.state["failure_streak"] == 0


# -- G. recovery migrations ------------------------------------------------------

def test_stale_lock_recovery_is_audited(sandbox):
    """run_cycle's own ProjectLock() uses the default 7200s staleness
    threshold, so this backdates the lock file's mtime (via os.utime)
    rather than actually sleeping past it."""
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    lock = projstate.ProjectLock(a)
    assert lock.acquire() is True
    os.close(lock.fd)   # simulate the holding process crashing
    lock.fd = None
    old = __import__("time").time() - 7300
    os.utime(lock.path, (old, old))
    caller = std_caller(task)
    result = cycle(sandbox, caller, Clock())
    assert result["status"] in ("success", "failure")
    log_path = sandbox["agentic"] / "memory" / "decisions.jsonl"
    import json as _json
    events = [_json.loads(line) for line in
             log_path.read_text(encoding="utf-8").splitlines() if line]
    assert any(e.get("event") == "stale_lock_recovered" for e in events)


def test_project_lock_broke_stale_lock_flag(tmp_path):
    agentic = tmp_path / "agentic"
    (agentic / "project").mkdir(parents=True)
    lock1 = projstate.ProjectLock(str(agentic), stale_seconds=1)
    assert lock1.acquire() is True
    assert lock1.broke_stale_lock is False
    os.close(lock1.fd)   # simulate the holding process crashing
    lock1.fd = None
    import time
    time.sleep(1.1)
    lock2 = projstate.ProjectLock(str(agentic), stale_seconds=1)
    assert lock2.acquire() is True
    assert lock2.broke_stale_lock is True
    assert lock2.broke_stale_lock_age_seconds >= 1
