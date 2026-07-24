"""Fixes the live bootstrap failure from run 20260722-181213:
expected_paths was wrongly enforced as a second write allowlist (a
normal scaffold-support file like .gitignore, already inside
allowed_paths, failed structural validation for not ALSO being in
expected_paths), and the worker had no primitive to create a bare
required directory. allowed_paths remains the ONLY write security
boundary throughout; expected_paths becomes a pure acceptance contract."""
import json
import os
import subprocess

import pytest

from conftest import (Clock, FakeCaller, git, project_cfg, proj_order,
                      seed_project, simple_task, worker_out)
from core import bootstrap_gate, errors, gitops, projstate
from core.orchestrator import apply_edits
from core.project import run_cycle


def qa_pass():
    return {"verdict": "pass", "done_when_results": [], "reason": "ok",
            "out_of_scope_changes": [], "test_integrity_preserved": True}


def sec_pass():
    return {"verdict": "pass", "concerns": [], "reason": "clean"}


def std_caller(task, coder=None, extra=None):
    scripted = {"conductor": proj_order(task), "coder": coder or worker_out(),
               "qa": qa_pass(), "security": sec_pass()}
    scripted.update(extra or {})
    return FakeCaller(scripted)


def cycle(sandbox, caller, clock, run_id=None):
    return run_cycle(sandbox["cfg"], caller=caller, clock=clock,
                     run_id=run_id)


# -- gitops.safe_makedirs: the directory-creation primitive -------------------

def test_safe_makedirs_creates_directory_covered_by_allowed_paths(tmp_path):
    worktree = str(tmp_path)
    full = gitops.safe_makedirs(worktree, "src", ["src/**"], [])
    assert os.path.isdir(full)
    assert full == os.path.realpath(os.path.join(worktree, "src"))


def test_safe_makedirs_is_idempotent(tmp_path):
    worktree = str(tmp_path)
    first = gitops.safe_makedirs(worktree, "src", ["src/**"], [])
    second = gitops.safe_makedirs(worktree, "src", ["src/**"], [])
    assert first == second
    assert os.path.isdir(first)


def test_safe_makedirs_blocks_directory_outside_allowed_paths(tmp_path):
    with pytest.raises(errors.PolicyError, match="allowed_paths"):
        gitops.safe_makedirs(str(tmp_path), "src", ["docs/**"], [])


def test_safe_makedirs_blocks_traversal_outside_worktree(tmp_path):
    with pytest.raises(errors.PolicyError):
        gitops.safe_makedirs(str(tmp_path), "../escape", ["**"], [])


def test_safe_makedirs_blocks_windows_absolute_drive_path(tmp_path):
    with pytest.raises(errors.PolicyError, match="absolute"):
        gitops.safe_makedirs(str(tmp_path), "C:/Windows/Temp/evil", ["**"], [])


def test_safe_makedirs_blocks_unc_path(tmp_path):
    with pytest.raises(errors.PolicyError, match="absolute"):
        gitops.safe_makedirs(str(tmp_path), r"\\evilserver\share\folder",
                             ["**"], [])


def test_safe_makedirs_blocks_reserved_device_name(tmp_path):
    with pytest.raises(errors.PolicyError, match="reserved"):
        gitops.safe_makedirs(str(tmp_path), "src/CON", ["**"], [])


def test_safe_makedirs_blocks_protected_path(tmp_path):
    with pytest.raises(errors.PolicyError, match="protected"):
        gitops.safe_makedirs(str(tmp_path), ".agentic/core", ["**"],
                             [".agentic/core/**", ".agentic/core"])


def test_safe_makedirs_blocks_directory_outside_worktree_via_junction(
        tmp_path):
    """Windows junction escape: worktree/link -> an outside directory. A
    directory-creation request through the junction must still be caught
    by safe_join's realpath containment check, exactly like a symlink."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = worktree / "escape_link"
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip("junctions unavailable in this environment: %s"
                    % proc.stderr)
    with pytest.raises(errors.PolicyError, match="escapes"):
        gitops.safe_makedirs(str(worktree), "escape_link/evil_subdir",
                             ["**"], [])


def test_safe_join_blocks_symlink_escape(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = worktree / "link"
    try:
        os.symlink(str(outside), str(link), target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("symlinks unavailable in this environment: %s" % exc)
    with pytest.raises(errors.PolicyError, match="escapes"):
        gitops.safe_join(str(worktree), "link/evil.txt")


# -- ensure_parent_dir / apply_edits: authorised parent creation --------------

def test_ensure_parent_dir_creates_authorised_parents_for_file_write(
        tmp_path):
    worktree = str(tmp_path)
    full = gitops.safe_join(worktree, "src/nested/index.js")
    parent = gitops.ensure_parent_dir(worktree, full)
    assert os.path.isdir(parent)
    assert parent == os.path.realpath(os.path.join(worktree, "src/nested"))


def test_apply_edits_write_auto_creates_missing_parents(tmp_path):
    worktree = str(tmp_path)
    violations = apply_edits(
        worktree, [{"path": "src/index.js", "action": "write",
                    "content": "x"}], ["src/**"], [], [])
    assert violations == []
    assert os.path.isfile(os.path.join(worktree, "src", "index.js"))


def test_apply_edits_mkdir_action_creates_directory(tmp_path):
    worktree = str(tmp_path)
    violations = apply_edits(worktree, [{"path": "src", "action": "mkdir"}],
                             ["src/**"], [], [])
    assert violations == []
    assert os.path.isdir(os.path.join(worktree, "src"))


def test_apply_edits_mkdir_blocked_outside_allowed_paths(tmp_path):
    worktree = str(tmp_path)
    violations = apply_edits(worktree, [{"path": "docs", "action": "mkdir"}],
                             ["src/**"], [], [])
    assert violations
    assert not os.path.isdir(os.path.join(worktree, "docs"))


# -- bootstrap_gate expected-outputs vs. additional-files ----------------------

def test_gitignore_in_allowed_not_expected_paths_passes(tmp_path):
    """The exact live bug: .gitignore is a normal changed file (within
    allowed_paths) but is not one of the required expected_paths -- it
    must never fail bootstrap-expected-outputs."""
    git(["init", "-b", "main"], tmp_path)
    git(["config", "user.email", "t@t"], tmp_path)
    git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.js").write_text("console.log(1)",
                                                encoding="utf-8")
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(["add", "-A"], tmp_path)
    task = {"expected_paths": [
        {"path": "src", "type": "directory", "required": True},
        {"path": "src/index.js", "type": "file", "required": True,
         "non_empty": True}]}
    result = bootstrap_gate.run_structural_checks(task, str(tmp_path))
    outputs = next(r for r in result["results"]
                  if r["name"] == "bootstrap-expected-outputs")
    assert outputs["passed"] is True
    assert result["ok"] is True
    assert result["tests"] == "not_configured_yet"


def test_additional_allowed_scaffold_file_reported_in_evidence():
    task = {"expected_paths": ["src/index.js"]}
    result = bootstrap_gate._check_additional_files(
        task, ["src/index.js", ".gitignore", "README.md"])
    assert result["passed"] is True
    assert result["mandatory"] is False
    assert result["applicable"] is True
    assert ".gitignore" in result["detail"]
    assert "README.md" in result["detail"]


def test_no_additional_files_not_applicable():
    task = {"expected_paths": ["src/index.js"]}
    result = bootstrap_gate._check_additional_files(task, ["src/index.js"])
    assert result["applicable"] is False


# -- missing/wrong-type/empty expected outputs ---------------------------------

def test_missing_expected_directory_blocks(tmp_path):
    task = {"expected_paths": [{"path": "src", "type": "directory",
                                "required": True}]}
    result = bootstrap_gate._check_expected_outputs(task, str(tmp_path), [])
    assert result["passed"] is False
    assert "missing_directory" in result["detail"]


def test_expected_file_existing_as_directory_blocks(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.js").mkdir()   # a directory, not a file
    task = {"expected_paths": [{"path": "src/index.js", "type": "file",
                                "required": True}]}
    result = bootstrap_gate._check_expected_outputs(
        task, str(tmp_path), ["src/index.js"])
    assert result["passed"] is False
    assert "wrong_type" in result["detail"]


def test_expected_directory_existing_as_file_blocks(tmp_path):
    (tmp_path / "src").write_text("oops, this is a file", encoding="utf-8")
    task = {"expected_paths": [{"path": "src", "type": "directory",
                                "required": True}]}
    result = bootstrap_gate._check_expected_outputs(
        task, str(tmp_path), ["src"])
    assert result["passed"] is False
    assert "wrong_type" in result["detail"]


def test_empty_required_file_blocks(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.js").write_text("", encoding="utf-8")
    task = {"expected_paths": [{"path": "src/index.js", "type": "file",
                                "required": True, "non_empty": True}]}
    result = bootstrap_gate._check_expected_outputs(
        task, str(tmp_path), ["src/index.js"])
    assert result["passed"] is False
    assert "empty_required_file" in result["detail"]


def test_legacy_string_expected_paths_remain_compatible(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.js").write_text("x", encoding="utf-8")
    task = {"expected_paths": ["src/", "src/index.js", "src/*.js"]}
    result = bootstrap_gate._check_expected_outputs(
        task, str(tmp_path), ["src/index.js"])
    assert result["passed"] is True


def test_gitignore_dangerous_negation_pattern_blocked(tmp_path):
    (tmp_path / ".gitignore").write_text(
        ".env\n!.env\n", encoding="utf-8")
    result = bootstrap_gate._check_gitignore_safe(
        str(tmp_path), [".gitignore"])
    assert result["passed"] is False
    assert "!.env" in result["detail"]


def test_gitignore_safe_negation_not_flagged(tmp_path):
    (tmp_path / ".gitignore").write_text(
        "node_modules/\n!node_modules/keep-this.txt\n", encoding="utf-8")
    result = bootstrap_gate._check_gitignore_safe(
        str(tmp_path), [".gitignore"])
    assert result["passed"] is True


# -- impossible-contract pre-check --------------------------------------------

def test_validate_task_contract_accepts_coverable_outputs():
    task = {"expected_paths": [
        {"path": "src", "type": "directory", "required": True},
        {"path": "src/index.js", "type": "file", "required": True}]}
    assert bootstrap_gate.validate_task_contract(task, ["src/**"]) == []


def test_validate_task_contract_rejects_impossible_outputs():
    task = {"expected_paths": [
        {"path": "src/index.js", "type": "file", "required": True}]}
    problems = bootstrap_gate.validate_task_contract(task, ["docs/**"])
    assert problems and "src/index.js" in problems[0]


# -- full-cycle integration: the exact live bug, fixed -------------------------

def test_gitignore_and_extra_scaffold_files_do_not_block_cycle(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    task = simple_task(
        kind="bootstrap",
        expected_paths=[{"path": "src", "type": "directory",
                         "required": True},
                        {"path": "src/index.js", "type": "file",
                         "required": True, "non_empty": True}],
        expected_size="small")
    test_setup = simple_task("t2-tests", kind="test_setup",
                             dependencies=["t1-first"])
    seed_project(sandbox, [task, test_setup])
    caller = std_caller(task, coder=worker_out(edits=[
        {"path": "src/index.js", "action": "write",
         "content": "console.log(1)\n"},
        {"path": ".gitignore", "action": "write",
         "content": "node_modules/\n"}]))
    caller.by_role["conductor"] = [proj_order(
        task, allowed_paths=["src/**", ".gitignore"])]
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "success"
    tasks = {t["id"]: t for t in projstate.load_backlog(
        str(sandbox["agentic"]))}
    assert tasks["t1-first"]["status"] == "done"


def test_additional_file_outside_allowed_paths_still_blocked(sandbox):
    """allowed_paths remains the real, unweakened security boundary --
    the expected_paths fix never widens what may be WRITTEN."""
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    task = simple_task(kind="bootstrap",
                       expected_paths=[{"path": "src/index.js",
                                       "type": "file", "required": True}])
    test_setup = simple_task("t2-tests", kind="test_setup",
                             dependencies=["t1-first"])
    seed_project(sandbox, [task, test_setup])
    caller = std_caller(task, coder=worker_out(edits=[
        {"path": "src/index.js", "action": "write", "content": "x"},
        {"path": "outside_scope.py", "action": "write", "content": "evil"}]))
    caller.by_role["conductor"] = [proj_order(task,
                                              allowed_paths=["src/**"])]
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "failure"
    tasks = projstate.load_backlog(str(sandbox["agentic"]))
    t1 = next(t for t in tasks if t["id"] == "t1-first")
    assert t1["status"] == "blocked"
    assert "outside_scope.py" not in os.listdir(
        str(sandbox["agentic"] / "worktrees" / "project"))


def test_required_directory_created_via_mkdir_in_full_cycle(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    task = simple_task(
        kind="bootstrap",
        expected_paths=[{"path": "src", "type": "directory",
                         "required": True}],
        expected_size="small")
    test_setup = simple_task("t2-tests", kind="test_setup",
                             dependencies=["t1-first"])
    seed_project(sandbox, [task, test_setup])
    caller = std_caller(task, coder=worker_out(edits=[
        {"path": "src", "action": "mkdir"},
        {"path": ".gitignore", "action": "write", "content": "x\n"}]))
    caller.by_role["conductor"] = [proj_order(
        task, allowed_paths=["src/**", ".gitignore"])]
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "success"


def test_impossible_task_contract_rejected_before_backend_invocation(
        sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    task = simple_task(
        kind="bootstrap",
        expected_paths=[{"path": "src/index.js", "type": "file",
                         "required": True}])
    test_setup = simple_task("t2-tests", kind="test_setup",
                             dependencies=["t1-first"])
    seed_project(sandbox, [task, test_setup])
    caller = std_caller(task)
    # the conductor scopes allowed_paths to something that can NEVER
    # cover the required expected output -- an impossible contract
    caller.by_role["conductor"] = [proj_order(task,
                                              allowed_paths=["docs/**"])]
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "failure"
    assert not [c for c in caller.calls if c["role"] == "coder"]
    tasks = projstate.load_backlog(str(sandbox["agentic"]))
    assert tasks[0]["status"] == "blocked"
    assert "platform_invalid" in tasks[0]["blocking_reason"]
    assert "not coverable by allowed_paths" in tasks[0]["blocking_reason"]


def test_structural_success_reports_tests_not_configured_yet(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    task = simple_task(
        kind="bootstrap",
        expected_paths=[{"path": "src/index.js", "type": "file",
                         "required": True}],
        expected_size="small")
    test_setup = simple_task("t2-tests", kind="test_setup",
                             dependencies=["t1-first"])
    seed_project(sandbox, [task, test_setup])
    caller = std_caller(task, coder=worker_out(edits=[
        {"path": "src/index.js", "action": "write", "content": "x"}]))
    caller.by_role["conductor"] = [proj_order(task,
                                              allowed_paths=["src/**"])]
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "success"
    assert "not_configured_yet" in result["detail"]


# -- recovery: the exact live blocker self-heals --------------------------------

def test_live_expected_paths_contract_bug_self_heals(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    task = simple_task("t1-init-repo", kind="bootstrap",
                       expected_paths=[{"path": "src", "type": "directory",
                                       "required": True},
                                      {"path": "src/index.js",
                                       "type": "file", "required": True,
                                       "non_empty": True}],
                       expected_size="small")
    test_setup = simple_task("t2-tests", kind="test_setup",
                             dependencies=["t1-init-repo"])
    seed_project(sandbox, [task, test_setup])
    a = str(sandbox["agentic"])
    live_reason = ("Validation check 'bootstrap-expected-paths' reports "
                  ".gitignore is outside task's expected_paths despite "
                  "being in allowed_paths. Cannot create src/ directory "
                  "through file write operations.")
    projstate.update_task(a, "t1-init-repo", status="blocked",
                          blocking_reason=live_reason)
    projstate.add_blocker(a, "t1-init-repo", live_reason, human_only=False)

    events = bootstrap_gate.recover_expected_paths_contract_bug(a)
    assert [e["task_id"] for e in events] == ["t1-init-repo"]
    task_after = next(t for t in projstate.load_backlog(a)
                      if t["id"] == "t1-init-repo")
    assert task_after["status"] == "pending"
    assert task_after["status"] != "done"
    assert task_after["blocking_reason"] is None
    blockers = projstate.read_yaml(a, "blockers.yaml", {}).get(
        "blockers", [])
    live_blockers = [b for b in blockers if b["task"] == "t1-init-repo"]
    assert live_blockers and all(b["resolved"] for b in live_blockers)

    caller = std_caller(task, coder=worker_out(edits=[
        {"path": "src/index.js", "action": "write",
         "content": "console.log(1)\n"},
        {"path": ".gitignore", "action": "write",
         "content": "node_modules/\n"}]))
    caller.by_role["conductor"] = [proj_order(
        task, allowed_paths=["src/**", ".gitignore"])]
    result = cycle(sandbox, caller, Clock(), run_id="recovery-1")
    assert result["status"] == "success"
    assert [c["role"] for c in caller.calls if c["role"] == "coder"]
    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t1-init-repo"]["status"] == "done"


def test_recovery_ran_before_full_cycle_logged_in_decisions(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    task = simple_task("t1-init-repo", kind="bootstrap",
                       expected_paths=[{"path": "src/index.js",
                                       "type": "file", "required": True}],
                       expected_size="small")
    test_setup = simple_task("t2-tests", kind="test_setup",
                             dependencies=["t1-init-repo"])
    seed_project(sandbox, [task, test_setup])
    a = str(sandbox["agentic"])
    live_reason = ("outside task's expected_paths despite being in "
                  "allowed_paths")
    projstate.update_task(a, "t1-init-repo", status="blocked",
                          blocking_reason=live_reason)
    caller = std_caller(task, coder=worker_out(edits=[
        {"path": "src/index.js", "action": "write", "content": "x"}]))
    caller.by_role["conductor"] = [proj_order(task,
                                              allowed_paths=["src/**"])]
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "success"
    log_path = sandbox["agentic"] / "memory" / "decisions.jsonl"
    events = [json.loads(line) for line in
             log_path.read_text(encoding="utf-8").splitlines() if line]
    assert any(e.get("event") == "expected_paths_contract_bug_recovered"
              for e in events)


# -- cooling transparency (item 11) --------------------------------------------

def test_cooldown_breakdown_explains_exponential_backoff(base_cfg, tmp_path):
    from core.scheduler import Scheduler
    scheduler = Scheduler(project_cfg({"cfg": base_cfg}), str(tmp_path / "m"))
    breakdown = scheduler.cooldown_breakdown("failure", failure_streak=2)
    assert breakdown["configured_after_failure_minutes"] == 30
    assert breakdown["backoff_multiplier"] == 2.0
    assert breakdown["clamped_minutes"] == 60
    assert "exponential_backoff" in breakdown["source"]


def test_cooldown_breakdown_first_failure_no_backoff(base_cfg, tmp_path):
    from core.scheduler import Scheduler
    scheduler = Scheduler(project_cfg({"cfg": base_cfg}), str(tmp_path / "m"))
    breakdown = scheduler.cooldown_breakdown("failure", failure_streak=1)
    assert breakdown["backoff_multiplier"] == 1.0
    assert breakdown["clamped_minutes"] == 30
    assert breakdown["source"] == "configured_failure_cooldown"


def test_start_cooling_persists_and_surfaces_breakdown(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    task = simple_task()
    seed_project(sandbox, [task])
    clock = Clock()
    result = cycle(sandbox, std_caller(task, coder=worker_out(edits=[])),
                   clock)
    assert result["status"] == "failure"
    assert result["cooling_detail"]["configured_after_failure_minutes"] == 30
    from core.scheduler import Scheduler
    scheduler = Scheduler(sandbox["cfg"], str(sandbox["agentic"] / "memory"))
    assert scheduler.state["cooling_detail"]["source"]
