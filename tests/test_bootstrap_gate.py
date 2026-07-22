"""Bootstrap deadlock fix: a scaffolding task with no test framework yet
must never be blocked forever by "zero deterministic checks", but "no
checks configured" must also never be reported as "tests passed"."""
import json

from conftest import (Clock, FakeCaller, git, project_cfg, seed_project,
                      simple_task, verifier_out, worker_out)
from core import bootstrap_gate, gitops, projstate
from core.project import run_cycle


def edit_ok(**over):
    return worker_out(**over)


def qa_pass():
    return verifier_out("pass")


def sec_pass():
    return {"verdict": "pass", "concerns": [], "reason": "clean"}


def std_caller(task, extra=None, qa=None, coder=None):
    from conftest import proj_order
    scripted = {"conductor": proj_order(task),
               "coder": coder or edit_ok(),
               "qa": qa or qa_pass(),
               "security": sec_pass()}
    scripted.update(extra or {})
    return FakeCaller(scripted)


def cycle(sandbox, caller, clock, run_id=None):
    return run_cycle(sandbox["cfg"], caller=caller, clock=clock,
                     run_id=run_id)


def bootstrap_and_test_setup_tasks():
    t1 = simple_task("t1-bootstrap", kind="bootstrap")
    t2 = simple_task("t2-tests", kind="test_setup",
                     dependencies=["t1-bootstrap"],
                     expected_paths=["tests/**"])
    return t1, t2


# 1. empty project + bootstrap task + valid created files -> may pass ---------
def test_bootstrap_task_with_valid_files_passes_structurally(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    t1, t2 = bootstrap_and_test_setup_tasks()
    seed_project(sandbox, [t1, t2])
    result = cycle(sandbox, std_caller(t1), Clock(), run_id="c1")
    assert result["status"] == "success"
    assert "not_configured_yet" in result["detail"]
    tasks = {t["id"]: t for t in projstate.load_backlog(str(sandbox["agentic"]))}
    assert tasks["t1-bootstrap"]["status"] == "done"
    assert tasks["t1-bootstrap"]["last_result"] == "pass"


# 2. empty project + bootstrap task + no files created -> block ---------------
def test_bootstrap_task_with_no_files_blocks(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    t1, t2 = bootstrap_and_test_setup_tasks()
    seed_project(sandbox, [t1, t2])
    caller = std_caller(t1, coder=edit_ok(edits=[]))
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "failure"
    tasks = {t["id"]: t for t in projstate.load_backlog(str(sandbox["agentic"]))}
    assert tasks["t1-bootstrap"]["status"] == "blocked"


# 3. structural gate never reports ok with zero applicable checks -------------
def test_structural_gate_never_passes_with_zero_applicable_checks(
        monkeypatch, tmp_path):
    git(["init", "-b", "main"], tmp_path)
    git(["config", "user.email", "t@t"], tmp_path)
    git(["config", "user.name", "t"], tmp_path)

    def _not_applicable(*a, **kw):
        return bootstrap_gate._result("stub", "structural", True, True,
                                      "stubbed out", applicable=False)
    for name in ("_check_files_changed", "_check_files_non_empty",
                "_check_root_containment", "_check_expected_paths",
                "_check_git_valid", "_check_no_credentials",
                "_check_manifest_parses", "_check_entry_points_exist",
                "_check_html_structural", "_check_js_syntax"):
        monkeypatch.setattr(bootstrap_gate, name,
                            lambda *a, **kw: _not_applicable())
    result = bootstrap_gate.run_structural_checks({"expected_paths": []},
                                                  str(tmp_path))
    assert result["ok"] is False
    assert result["no_checks"] is True
    assert result["tests"] == "not_configured_yet"


# 4. bootstrap structural checks fail -> block (distinct from "no files") -----
def test_bootstrap_structural_failure_blocks(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    t1, t2 = bootstrap_and_test_setup_tasks()
    seed_project(sandbox, [t1, t2])
    caller = std_caller(t1, coder=edit_ok(edits=[
        {"path": "src/empty.py", "action": "write", "content": ""}]))
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "failure"
    tasks = {t["id"]: t for t in projstate.load_backlog(str(sandbox["agentic"]))}
    assert tasks["t1-bootstrap"]["status"] == "blocked"


# 5. business-logic task with no tests -> block (unaffected by the fix) -------
def test_business_logic_task_with_no_checks_still_blocks(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    task = simple_task()   # kind unset -> not bootstrap-eligible
    seed_project(sandbox, [task])
    result = cycle(sandbox, std_caller(task), Clock())
    assert result["status"] == "failure"
    assert "zero deterministic checks" in result["detail"]
    tasks = projstate.load_backlog(str(sandbox["agentic"]))
    assert tasks[0]["status"] == "blocked"


# 6. test framework configured but zero tests execute -> block ----------------
def test_configured_framework_with_zero_tests_blocks(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = "auto"
    (sandbox["repo"] / "tests").mkdir()
    (sandbox["repo"] / "tests" / "test_empty.py").write_text(
        "# no test functions yet\n", encoding="utf-8")
    git(["add", "-A"], sandbox["repo"])
    git(["commit", "-m", "add empty tests package"], sandbox["repo"])
    task = simple_task()
    seed_project(sandbox, [task])
    result = cycle(sandbox, std_caller(task), Clock())
    assert result["status"] == "failure"
    tasks = projstate.load_backlog(str(sandbox["agentic"]))
    assert tasks[0]["status"] == "blocked"
    assert "no deterministic checks" not in (
        tasks[0]["blocking_reason"] or "")


# 7. later test_setup task installs tests -> mandatory test-suite mode --------
def test_test_setup_task_transitions_to_mandatory_test_suite(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = "auto"
    clock = Clock()
    t1, t2 = bootstrap_and_test_setup_tasks()
    seed_project(sandbox, [t1, t2])

    r1 = cycle(sandbox, std_caller(t1), clock, run_id="c1")
    assert r1["status"] == "success"
    assert "not_configured_yet" in r1["detail"]

    clock.advance(minutes=31)
    caller2 = std_caller(t2, coder=edit_ok(edits=[
        {"path": "tests/test_basic.py", "action": "write",
         "content": "def test_ok():\n    assert True\n"}]))
    r2 = cycle(sandbox, caller2, clock, run_id="c2")
    assert r2["status"] == "success"
    assert "not_configured_yet" not in r2["detail"]
    tasks = {t["id"]: t for t in projstate.load_backlog(str(sandbox["agentic"]))}
    assert tasks["t2-tests"]["last_result"] == "pass"


# 8. a structural pass is reported as structural, never as a test pass --------
def test_structural_pass_never_reported_as_test_pass(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    t1, t2 = bootstrap_and_test_setup_tasks()
    seed_project(sandbox, [t1, t2])
    cycle(sandbox, std_caller(t1), Clock(), run_id="c1")
    log_path = sandbox["agentic"] / "memory" / "decisions.jsonl"
    events = [json.loads(line) for line in
             log_path.read_text(encoding="utf-8").splitlines() if line]
    gate_events = [e for e in events
                  if e.get("event") == "bootstrap_structural_gate"]
    assert gate_events
    assert gate_events[0]["tests"] == "not_configured_yet"
    assert gate_events[0]["ok"] is True


# 9. model/AI output can never override a deterministic failure ----------------
def test_ai_cannot_override_deterministic_structural_failure(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    t1, t2 = bootstrap_and_test_setup_tasks()
    seed_project(sandbox, [t1, t2])
    # QA is scripted to always say "pass" -- it must never even be asked,
    # because the deterministic structural gate fails first every time.
    caller = std_caller(t1, coder=edit_ok(edits=[
        {"path": "src/empty.py", "action": "write", "content": ""}]),
        qa=qa_pass())
    result = cycle(sandbox, caller, Clock())
    assert result["status"] == "failure"
    assert not [c for c in caller.calls if c["role"] == "qa"]


# 10. generated files outside the project root -> block -----------------------
def test_root_containment_check_blocks_path_escape(tmp_path):
    result = bootstrap_gate._check_root_containment(
        str(tmp_path), ["../outside.txt", "ok.txt"])
    assert result["passed"] is False
    assert "outside.txt" in result["detail"]


def test_expected_paths_check_blocks_files_outside_declared_scope():
    task = {"expected_paths": ["src/**"]}
    result = bootstrap_gate._check_expected_paths(
        task, ["src/app.py", "outside/evil.py"])
    assert result["passed"] is False
    assert "outside/evil.py" in result["detail"]


# 11 + 12: existing repair/handoff behaviour and the full suite are verified
# by running the pre-existing tests in tests/test_scheduler_project.py
# (test_repair_attempts_exhausted_blocks_task,
# test_structured_handoff_on_mid_task_exhaustion) and the full pytest run
# alongside this file -- no behaviour there changed by this fix.


# -- classification unit tests -------------------------------------------------
def test_bootstrap_eligible_requires_both_kind_and_scheduled_test_setup():
    bootstrap_task = simple_task("t1", kind="bootstrap")
    business_task = simple_task("t1")
    no_test_setup_backlog = [bootstrap_task]
    with_test_setup_backlog = [bootstrap_task,
                              simple_task("t2", kind="test_setup")]
    assert bootstrap_gate.bootstrap_eligible(
        bootstrap_task, no_test_setup_backlog)[0] is False
    assert bootstrap_gate.bootstrap_eligible(
        bootstrap_task, with_test_setup_backlog)[0] is True
    assert bootstrap_gate.bootstrap_eligible(
        business_task, with_test_setup_backlog)[0] is False


def test_recover_bootstrap_deadlock_unblocks_only_eligible_tasks(sandbox):
    project_cfg(sandbox)
    t1, t2 = bootstrap_and_test_setup_tasks()
    business = simple_task("t3-business")
    seed_project(sandbox, [t1, t2, business])
    a = str(sandbox["agentic"])
    projstate.update_task(a, "t1-bootstrap", status="blocked",
                          blocking_reason=bootstrap_gate.NO_CHECKS_BLOCKER_REASON)
    projstate.update_task(a, "t3-business", status="blocked",
                          blocking_reason=bootstrap_gate.NO_CHECKS_BLOCKER_REASON)
    projstate.add_blocker(a, "t1-bootstrap",
                          bootstrap_gate.NO_CHECKS_HUMAN_REASON,
                          human_only=True)
    projstate.add_blocker(a, "t3-business",
                          bootstrap_gate.NO_CHECKS_HUMAN_REASON,
                          human_only=True)
    recovered = bootstrap_gate.recover_bootstrap_deadlock(a)
    assert recovered == ["t1-bootstrap"]
    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t1-bootstrap"]["status"] == "pending"
    assert tasks["t1-bootstrap"]["blocking_reason"] is None
    assert tasks["t3-business"]["status"] == "blocked"
