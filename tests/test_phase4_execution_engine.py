"""Phase 4 -- Optional Orca Adapter.

Orca itself is not installed on this machine (confirmed: `shutil.which
("orca")` returns None here) -- so the REAL, live-verified behaviour this
phase proves is the fallback path (native runs automatically). The
"Orca present and compatible" path is verified against a test double:
`shutil.which` is monkeypatched to simulate Orca being on PATH, and a
dependency-injected `runner` callable stands in for the actual
subprocess call -- this tests the ADAPTER's own logic (version parsing,
event translation, legacy-dict construction, error handling) without
depending on Windows subprocess/batch-file quirks or a real Orca binary.
Nothing here copies or forks Orca source; the fake runner only ever
returns the same two conventions the adapter itself declares it depends
on (--version output, a --json result payload)."""
import json

import pytest

from conftest import Clock, FakeCaller, project_cfg, proj_order, seed_project, simple_task, worker_out
from core import execengine, gitops
from core.project import run_cycle


def qa_pass():
    return {"verdict": "pass", "done_when_results": [], "reason": "ok",
           "out_of_scope_changes": [], "test_integrity_preserved": True}


# -- interface contract ---------------------------------------------------------

def test_native_engine_implements_full_interface():
    engine = execengine.NativeExecutionEngine({}, caller=None)
    for method in ("installation_detected", "detect_version",
                  "version_supported", "probe_capabilities", "smoke_test",
                  "attach_worktree", "launch_agent", "poll_status",
                  "cancel", "collect_diff", "collect_result", "cleanup"):
        assert callable(getattr(engine, method))


def test_native_engine_always_available():
    engine = execengine.NativeExecutionEngine({}, caller=None)
    assert engine.installation_detected() is True
    assert engine.version_supported("anything") is True
    assert engine.probe_capabilities()["available"] is True


# -- selection / fallback (the live-verified path on this machine) ------------

def test_select_engine_defaults_to_native():
    cfg = {"execution": {"preferred_engine": "native"}, "orca": {"enabled": False}}
    engine, decision = execengine.select_engine(cfg)
    assert engine.name == execengine.ENGINE_NATIVE
    assert decision["selected"] == execengine.ENGINE_NATIVE


def test_select_engine_falls_back_when_orca_disabled():
    cfg = {"execution": {"preferred_engine": "orca"},
          "orca": {"enabled": False}}
    engine, decision = execengine.select_engine(cfg)
    assert engine.name == execengine.ENGINE_NATIVE
    assert "not preferred/enabled" in decision["reason"]


def test_select_engine_falls_back_when_orca_not_installed_on_this_machine():
    """The REAL state of this machine: no orca executable exists."""
    cfg = {"execution": {"preferred_engine": "orca"},
          "orca": {"enabled": True, "executable": "orca",
                  "supported_versions": ["9.9.9"],
                  "fallback_to_native": True}}
    engine, decision = execengine.select_engine(cfg)
    assert engine.name == execengine.ENGINE_NATIVE
    assert decision["selected"] == execengine.ENGINE_NATIVE
    assert "falling back to native" in decision["reason"]
    assert decision["capabilities"]["installed"] is False


def test_select_engine_raises_when_fallback_disabled_and_orca_unavailable():
    cfg = {"execution": {"preferred_engine": "orca"},
          "orca": {"enabled": True, "executable": "orca",
                  "supported_versions": [], "fallback_to_native": False}}
    with pytest.raises(execengine.EngineUnavailable):
        execengine.select_engine(cfg)


def test_select_engine_uses_orca_when_available_and_compatible(monkeypatch):
    monkeypatch.setattr(execengine.shutil, "which",
                        lambda exe: "/fake/orca" if exe == "orca" else None)
    fake_runner = lambda argv, cwd=None, timeout=30, **kw: {
        "exit_code": 0, "stdout": "orca version 2.3.1", "stderr": ""}
    cfg = {"execution": {"preferred_engine": "orca"},
          "orca": {"enabled": True, "executable": "orca",
                  "supported_versions": ["2.3.1"]}}
    engine, decision = execengine.select_engine(cfg, runner=fake_runner)
    assert engine.name == execengine.ENGINE_ORCA
    assert decision["selected"] == execengine.ENGINE_ORCA


# -- orca adapter: version/capability probing (dependency-injected) -----------

def test_orca_installation_detected_via_which(monkeypatch):
    monkeypatch.setattr(execengine.shutil, "which",
                        lambda exe: "/fake/orca" if exe == "orca" else None)
    engine = execengine.OrcaExecutionEngine({}, executable="orca")
    assert engine.installation_detected() is True


def test_orca_not_installed_when_which_finds_nothing(monkeypatch):
    monkeypatch.setattr(execengine.shutil, "which", lambda exe: None)
    engine = execengine.OrcaExecutionEngine({}, executable="orca")
    assert engine.installation_detected() is False
    assert engine.detect_version() is None
    caps = engine.probe_capabilities()
    assert caps == {"available": False, "installed": False,
                    "reason": "executable not found: orca"}


def test_orca_version_parsed_from_runner_output(monkeypatch):
    monkeypatch.setattr(execengine.shutil, "which",
                        lambda exe: "/fake/orca")
    runner = lambda argv, cwd=None, timeout=30, **kw: {
        "exit_code": 0, "stdout": "Orca CLI version 3.1.4 (build abc)\n",
        "stderr": ""}
    engine = execengine.OrcaExecutionEngine(
        {}, executable="orca", supported_versions=["3.1.4"], runner=runner)
    assert engine.detect_version() == "3.1.4"
    assert engine.version_supported("3.1.4") is True
    assert engine.version_supported("9.9.9") is False


def test_orca_explicit_empty_supported_versions_never_compatible(monkeypatch):
    """item B: an empty supported_versions list means nothing is
    supported yet, even if installed with a detectable version -- never
    'assume the latest works'."""
    monkeypatch.setattr(execengine.shutil, "which", lambda exe: "/fake/orca")
    runner = lambda argv, cwd=None, timeout=30, **kw: {
        "exit_code": 0, "stdout": "1.0.0", "stderr": ""}
    engine = execengine.OrcaExecutionEngine(
        {}, executable="orca", supported_versions=[], runner=runner)
    caps = engine.probe_capabilities()
    assert caps["available"] is False
    assert caps["installed"] is True


def test_orca_smoke_test_is_non_destructive(tmp_path, monkeypatch):
    monkeypatch.setattr(execengine.shutil, "which", lambda exe: "/fake/orca")
    runner = lambda argv, cwd=None, timeout=30, **kw: {
        "exit_code": 0, "stdout": "1.0.0", "stderr": ""}
    engine = execengine.OrcaExecutionEngine(
        {}, executable="orca", supported_versions=["1.0.0"], runner=runner)
    before = set(tmp_path.iterdir())
    assert engine.smoke_test(str(tmp_path)) is True
    after = set(tmp_path.iterdir())
    assert before == after   # nothing written to workdir


# -- orca adapter: launch_agent / event translation / result collection -------

def _compatible_orca(runner, supported="1.2.0"):
    import core.execengine as _e
    return _e.OrcaExecutionEngine({}, executable="orca",
                                  supported_versions=[supported],
                                  runner=lambda *a, **kw: {
                                      "exit_code": 0,
                                      "stdout": "orca %s" % supported,
                                      "stderr": ""} if a[0][1] == "--version"
                                  else runner(*a, **kw))


def test_orca_launch_agent_parses_json_result_and_events(monkeypatch, tmp_path):
    monkeypatch.setattr(execengine.shutil, "which", lambda exe: "/fake/orca")
    payload = {"events": [{"type": "file_written", "detail": "hello.txt"}],
              "edits": [{"path": "hello.txt", "action": "write",
                        "content": "hi"}],
              "blocked": False, "usage": {"input_tokens": 12,
                                          "output_tokens": 4}}

    def runner(argv, cwd=None, timeout=None, **kw):
        return {"exit_code": 0, "stdout": json.dumps(payload), "stderr": ""}

    engine = _compatible_orca(runner)
    request = execengine.SessionRequest(
        run_id="r1", task_id="t1", worktree=str(tmp_path), role="coder",
        coder_input={}, chain=["orca"])
    session = engine.launch_agent(request)
    assert session.status == execengine.STATUS_COMPLETED
    legacy = session.to_legacy_dict()
    assert legacy["ok"] is True
    assert legacy["edits"] == payload["edits"]
    assert legacy["blocked"] is False
    assert legacy["usage"]["input_tokens"] == 12
    assert session.events == [{"type": "file_written", "detail": "hello.txt"}]


def test_orca_launch_agent_blocked_result(monkeypatch, tmp_path):
    monkeypatch.setattr(execengine.shutil, "which", lambda exe: "/fake/orca")
    payload = {"events": [], "edits": [], "blocked": True,
              "blocker": "missing credential"}

    def runner(argv, cwd=None, timeout=None, **kw):
        return {"exit_code": 0, "stdout": json.dumps(payload), "stderr": ""}

    engine = _compatible_orca(runner)
    request = execengine.SessionRequest(
        run_id="r1", task_id="t1", worktree=str(tmp_path), role="coder",
        coder_input={}, chain=["orca"])
    session = engine.launch_agent(request)
    legacy = session.to_legacy_dict()
    assert legacy["ok"] is False
    assert legacy["blocked"] is True
    assert legacy["blocker"] == "missing credential"


def test_orca_malformed_output_becomes_raw_event_not_a_crash(monkeypatch,
                                                              tmp_path):
    """Never silently parses/guesses an undocumented format -- anything
    that isn't the declared --json shape becomes one opaque event."""
    monkeypatch.setattr(execengine.shutil, "which", lambda exe: "/fake/orca")

    def runner(argv, cwd=None, timeout=None, **kw):
        return {"exit_code": 0, "stdout": "not json at all", "stderr": ""}

    engine = _compatible_orca(runner)
    request = execengine.SessionRequest(
        run_id="r1", task_id="t1", worktree=str(tmp_path), role="coder",
        coder_input={}, chain=["orca"])
    session = engine.launch_agent(request)
    assert session.events == [{"type": "raw_output", "detail": "not json at all"}]
    legacy = session.to_legacy_dict()
    assert legacy["edits"] is None   # nothing parsed -- never guessed


def test_orca_launch_agent_raises_unavailable_when_not_compatible(tmp_path):
    engine = execengine.OrcaExecutionEngine(
        {}, executable="definitely-not-a-real-executable-xyz",
        supported_versions=["1.0.0"])
    request = execengine.SessionRequest(
        run_id="r1", task_id="t1", worktree=str(tmp_path), role="coder",
        coder_input={}, chain=["orca"])
    with pytest.raises(execengine.EngineUnavailable):
        engine.launch_agent(request)


def test_orca_timeout_produces_execution_timeout_status(monkeypatch, tmp_path):
    monkeypatch.setattr(execengine.shutil, "which", lambda exe: "/fake/orca")

    def runner(argv, cwd=None, timeout=None, **kw):
        if argv[1] == "--version":
            return {"exit_code": 0, "stdout": "1.0.0", "stderr": ""}
        # supervised runners (core.supervisor) never raise for a timeout
        # -- they always return a dict with timed_out=True, so the
        # timeout signal survives a bounded, guaranteed-to-return call
        return {"exit_code": None, "stdout": "", "stderr": "timed out",
               "timed_out": True}

    engine = execengine.OrcaExecutionEngine(
        {}, executable="orca", supported_versions=["1.0.0"], runner=runner)
    request = execengine.SessionRequest(
        run_id="r1", task_id="t1", worktree=str(tmp_path), role="coder",
        coder_input={}, chain=["orca"], timeout_seconds=1)
    session = engine.launch_agent(request)
    assert session.status == execengine.STATUS_TIMEOUT
    assert session.to_legacy_dict()["ok"] is False


def test_cancel_and_cleanup_are_safe_no_ops(tmp_path):
    engine = execengine.NativeExecutionEngine({}, caller=None)
    assert engine.cancel("no-such-session") is False
    engine.cleanup("no-such-session")   # never raises


# -- preserved-worktree recovery: both engines share the same primitive -------

def test_both_engines_attach_worktree_via_same_taskspace_primitive(
        sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    root = str(sandbox["repo"])
    agentic = str(sandbox["agentic"])
    native = execengine.NativeExecutionEngine({}, caller=None)
    orca = execengine.OrcaExecutionEngine({}, executable="orca")
    from core.project import ensure_project_worktree, PROJECT_BRANCH
    ensure_project_worktree(sandbox["cfg"], {
        "agentic": agentic, "root": root,
        "memory": str(sandbox["agentic"] / "memory"),
        "queue": str(sandbox["agentic"] / "queue"),
        "runs": str(sandbox["agentic"] / "runs")})
    path1 = native.attach_worktree(root, agentic, task["id"], PROJECT_BRANCH)
    path2 = orca.attach_worktree(root, agentic, task["id"], PROJECT_BRANCH)
    assert path1 == path2   # same worktree resumed, not recreated


# -- exit gate: native and orca produce equivalent validation evidence --------

def test_native_and_orca_produce_equivalent_validation_evidence(
        tmp_path, monkeypatch):
    """The literal Phase 4 exit gate: run the same deterministic task
    once through each engine and confirm the downstream deterministic
    gate reaches the SAME verdict on the edits each one produced."""
    import subprocess

    from core import errors as _errors, gate as gate_mod
    from core.orchestrator import apply_edits as _apply_edits

    def make_worktree(name):
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=str(repo),
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo),
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo),
                       check=True, capture_output=True)
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=str(repo),
                       check=True, capture_output=True)
        return repo

    allowed = ["hello.txt"]
    edits = [{"path": "hello.txt", "action": "write",
             "content": "hello from agentic os\n"}]

    # -- native: FakeCaller-backed
    native_repo = make_worktree("native")
    native_engine = execengine.NativeExecutionEngine(
        {"backends": {"mock": {"type": "api"}}},
        caller=FakeCaller({"coder": worker_out(edits=edits)}))
    native_session = native_engine.launch_agent(execengine.SessionRequest(
        run_id="r1", task_id="t1", worktree=str(native_repo), role="coder",
        coder_input={"work_order": {}}, chain=["mock"]))
    native_result = native_session.to_legacy_dict()
    _apply_edits(str(native_repo), native_result["edits"], allowed, [], [])
    gitops.stage_all(str(native_repo))

    # -- orca: fake-runner-backed, same edits
    monkeypatch.setattr(execengine.shutil, "which", lambda exe: "/fake/orca")
    orca_repo = make_worktree("orca")
    payload = {"events": [], "edits": edits, "blocked": False}

    def runner(argv, cwd=None, timeout=None, **kw):
        if argv[1] == "--version":
            return {"exit_code": 0, "stdout": "1.0.0", "stderr": ""}
        return {"exit_code": 0, "stdout": json.dumps(payload), "stderr": ""}

    orca_engine = execengine.OrcaExecutionEngine(
        {}, executable="orca", supported_versions=["1.0.0"], runner=runner)
    orca_session = orca_engine.launch_agent(execengine.SessionRequest(
        run_id="r1", task_id="t1", worktree=str(orca_repo), role="coder",
        coder_input={}, chain=["orca"]))
    orca_result = orca_session.to_legacy_dict()
    _apply_edits(str(orca_repo), orca_result["edits"], allowed, [], [])
    gitops.stage_all(str(orca_repo))

    # both engines produced the SAME file content
    assert (native_repo / "hello.txt").read_text(encoding="utf-8") == \
        (orca_repo / "hello.txt").read_text(encoding="utf-8")

    # the SAME deterministic gate reaches the SAME verdict for both
    cfg = {"verification": {"commands": [
        {"name": "ok", "command": "python -c \"import sys; sys.exit(0)\"",
         "mandatory": True}]}}
    native_gate = gate_mod.run_checks(cfg, str(native_repo))
    orca_gate = gate_mod.run_checks(cfg, str(orca_repo))
    assert native_gate["ok"] == orca_gate["ok"] is True
    assert native_gate["tests"] == orca_gate["tests"]


# -- full cycle wiring: default config uses native, unchanged behaviour -------

def test_default_project_cycle_selects_native_engine_and_logs_it(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = [
        {"name": "ok", "command": "python -c \"import sys; sys.exit(0)\"",
         "mandatory": True}]
    task = simple_task()
    from core import projstate
    projstate.write_yaml(str(sandbox["agentic"]), "milestones.yaml",
                         {"milestones": [{"id": "m1", "title": "m"}]})
    projstate.save_backlog(str(sandbox["agentic"]),
                           [projstate.normalize_task(task)])
    projstate.write_yaml(str(sandbox["agentic"]), "acceptance-criteria.yaml",
                         {"requirements_map": [],
                          "completion_criteria": ["all pass"]})
    projstate.write_yaml(str(sandbox["agentic"]), "decisions.yaml",
                         {"human_decisions_needed": [], "decided": []})
    projstate.write_yaml(str(sandbox["agentic"]), "blockers.yaml",
                         {"blockers": []})
    projstate.write_text(str(sandbox["agentic"]), "PROJECT.md", "# plan")
    projstate.write_text(str(sandbox["agentic"]), "architecture.md", "# arch")
    projstate.refresh_progress(str(sandbox["agentic"]))

    caller = FakeCaller({
        "conductor": proj_order(task),
        "coder": worker_out(edits=[{"path": "src/app.py", "action": "write",
                                   "content": "VALUE = 2\n"}]),
        "qa": qa_pass(),
        "security": {"verdict": "pass", "concerns": [], "reason": "clean"}})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "success"
    log_path = sandbox["agentic"] / "memory" / "decisions.jsonl"
    events = [json.loads(line) for line in
             log_path.read_text(encoding="utf-8").splitlines() if line]
    engine_events = [e for e in events
                    if e.get("event") == "execution_engine_selected"]
    assert engine_events and engine_events[0]["selected"] == "native"
