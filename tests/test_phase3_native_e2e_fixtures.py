"""Phase 3 -- Native End-to-End Proof.

Every fixture drives the REAL execution policy: real git worktrees
(taskspace.py), the real deterministic gate (gate.py/bootstrap_gate.py),
the real task contract/preflight (contract.py/preflight.py), real
commits, and a real final audit. Only the MODEL is replaced (FakeCaller)
-- deterministic, fast, no network, no quota consumed. This is
"mocked-model, real-platform" proof, exactly what a hermetic default
test suite can responsibly exercise; item 11 (a live native Ollama
round-trip) is a separate, explicitly opt-in fixture in
test_phase3_live_ollama.py that is skipped by default.

Fixtures 1 (static task manager) and 3 (Python CLI + pytest) run a REAL
`python -m pytest -q` inside the sandboxed worktree -- pytest is always
available in this environment, so there is no substitute needed there.
Fixtures that would need a real npm/vitest toolchain (2, 5) use an
explicit, clearly-labelled safe substitute verification command instead
of actually invoking npm -- installing real node_modules would require
network access, which the default suite never does. The project
SHAPE/orchestration those fixtures produce is still fully real.
"""
import os

from conftest import Clock, FakeCaller, project_cfg
from core import bootstrap_gate, projstate
from core.project import final_audit, project_start, run_cycle


def qa_pass():
    return {"verdict": "pass", "done_when_results": [], "reason": "ok",
           "out_of_scope_changes": [], "test_integrity_preserved": True}


def sec_pass():
    return {"verdict": "pass", "concerns": [], "reason": "clean"}


def order_for(task, **over):
    order = {"action": "execute", "item": task["description"],
             "skill": task.get("skill") or task["id"],
             "spec": task["description"],
             "done_when": [{"id": "DW-1", "condition": "criteria met",
                            "command": None}],
             "allowed_paths": bootstrap_gate.expected_path_strings(task),
             "forbidden_paths": [], "maximum_changed_lines": 200,
             "risk": task.get("risk", "low"), "queue_reason": None}
    order.update(over)
    return order


def run_project_to_completion(sandbox, architect_out, callers_by_task,
                              max_cycles=15, extra_qa=None):
    """Drive project_start then repeated run_cycle calls (advancing the
    clock past cooling each time) until final_audit reports "complete"
    or max_cycles is exhausted. `callers_by_task[task_id]` may be a
    FakeCaller or a list of FakeCallers consumed one per cycle (for a
    task that needs more than one attempt, e.g. a repair fixture)."""
    cfg = sandbox["cfg"]
    clock = Clock()
    plan = sandbox["repo"] / "plan.md"
    plan.write_text("# fixture plan\n", encoding="utf-8")
    start = project_start(cfg, str(plan),
                          caller=FakeCaller({"architect": architect_out}),
                          clock=clock)
    assert start["status"] == "started", start
    a = str(sandbox["agentic"])
    results = []
    for _ in range(max_cycles):
        task = projstate.next_task(a)
        clock.advance(minutes=31)
        if task is None:
            progress = projstate.refresh_progress(a)
            if progress["backlog_complete"]:
                r = run_cycle(cfg, caller=FakeCaller(
                    {"qa": extra_qa or qa_pass()}), clock=clock)
                results.append(r)
                return r, results
            r = run_cycle(cfg, caller=FakeCaller({}), clock=clock)
            results.append(r)
            return r, results
        scripted = callers_by_task[task["id"]]
        caller = scripted.pop(0) if isinstance(scripted, list) else scripted
        r = run_cycle(cfg, caller=caller, clock=clock)
        results.append(r)
    return results[-1] if results else None, results


def _worker(edits, blocked=False, blocker=None):
    return {"summary": "did it", "blocked": blocked, "blocker": blocker,
           "edits": edits, "commands": []}


def _caller(task, edits, coder=None):
    # UI-shaped paths (.jsx/.tsx/.css/ui/...) route to the ui_designer
    # role instead of coder (see project._worker_role) -- script both so
    # a fixture never has to know which one the platform will pick.
    worker_response = coder or _worker(edits)
    return FakeCaller({
        "conductor": order_for(task),
        "coder": worker_response, "ui_designer": worker_response,
        "qa": qa_pass(), "security": sec_pass()})


# -- 1. static task manager (real pytest) ---------------------------------------

def test_static_task_manager_completes_natively(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = "auto"
    architect_out = {
        "architecture": "static HTML/CSS/JS task manager, no build step",
        "assumptions": [],
        "milestones": [{"id": "m1", "title": "scaffold+logic"}],
        "backlog": [
            {"id": "t1-scaffold", "milestone": "m1",
             "description": "scaffold the static task manager and its "
                            "test framework", "dependencies": [],
             "risk": "low", "security_relevant": False,
             "expected_paths": [
                 {"path": "index.html", "type": "file", "required": True,
                  "non_empty": True}],
             "expected_size": "medium", "acceptance_criteria": ["scaffolded"],
             "deterministic_checks": ["python -m pytest -q"],
             "skill": "scaffold", "kind": "bootstrap"},
            {"id": "t2-tests", "milestone": "m1",
             "description": "add a passing pytest smoke test",
             "dependencies": ["t1-scaffold"], "risk": "low",
             "security_relevant": False,
             "expected_paths": [{"path": "tests/test_smoke.py",
                                 "type": "file", "required": True}],
             "expected_size": "small", "acceptance_criteria": ["tests pass"],
             "deterministic_checks": ["python -m pytest -q"],
             "skill": "tests", "kind": "test_setup"},
        ],
        "requirements_map": [], "completion_criteria": ["all checks pass"],
        "human_decisions": [],
    }
    t1 = architect_out["backlog"][0]
    t2 = architect_out["backlog"][1]
    callers = {
        "t1-scaffold": _caller(t1, [
            {"path": "index.html", "action": "write",
             "content": "<html><body><div id='app'></div></body></html>"}]),
        "t2-tests": _caller(t2, [
            {"path": "tests/test_smoke.py", "action": "write",
             "content": "def test_ok():\n    assert True\n"}]),
    }
    result, cycles = run_project_to_completion(sandbox, architect_out, callers)
    assert result["status"] == "complete", (result, cycles)
    audit = projstate.read_yaml(str(sandbox["agentic"]), "final-audit.yaml")
    assert audit["complete"] is True
    assert audit["checks"]["deterministic_checks_pass"] is True
    assert audit["checks"]["no_committed_secrets"] is True
    log = (sandbox["agentic"] / "memory" / "notifications.log").read_text()
    assert "project_complete" in log   # ready-for-review notification


# -- 3. Python CLI with pytest (real pytest) -------------------------------------

def test_python_cli_with_pytest_completes_natively(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = "auto"
    architect_out = {
        "architecture": "python CLI tool packaged with pytest",
        "assumptions": [], "milestones": [{"id": "m1", "title": "core"}],
        "backlog": [
            {"id": "t1-cli", "milestone": "m1",
             "description": "implement the CLI entry point and its tests",
             "dependencies": [], "risk": "low", "security_relevant": False,
             "expected_paths": [
                 {"path": "cli.py", "type": "file", "required": True,
                  "non_empty": True},
                 {"path": "tests/test_cli.py", "type": "file",
                  "required": True}],
             "expected_size": "medium",
             "acceptance_criteria": ["cli.py exposes main()"],
             "deterministic_checks": ["python -m pytest -q"],
             "skill": "cli"},
        ],
        "requirements_map": [], "completion_criteria": ["all checks pass"],
        "human_decisions": [],
    }
    t1 = architect_out["backlog"][0]
    callers = {"t1-cli": _caller(t1, [
        {"path": "cli.py", "action": "write",
         "content": "def main():\n    return 0\n"},
        {"path": "tests/test_cli.py", "action": "write",
         "content": "from cli import main\n\n\ndef test_main():\n"
                    "    assert main() == 0\n"}])}
    result, cycles = run_project_to_completion(sandbox, architect_out, callers)
    assert result["status"] == "complete", (result, cycles)


# -- 6. existing repository modification -----------------------------------------

def test_existing_repository_modification_completes_natively(sandbox):
    """The sandbox repo already has real, pre-existing content (from the
    `sandbox` fixture: src/app.py) -- this task modifies it rather than
    scaffolding fresh, and the user's pre-existing untracked file must
    survive untouched."""
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = [
        {"name": "ok", "command": "python -c \"import sys; sys.exit(0)\"",
         "mandatory": True}]
    dirty = sandbox["repo"] / "untouched_by_agent.txt"
    dirty.write_text("user's own in-progress work\n", encoding="utf-8")
    architect_out = {
        "architecture": "extend the existing app", "assumptions": [],
        "milestones": [{"id": "m1", "title": "extend"}],
        "backlog": [
            {"id": "t1-extend", "milestone": "m1",
             "description": "set VALUE to 2 in the existing src/app.py",
             "dependencies": [], "risk": "low", "security_relevant": False,
             "expected_paths": [{"path": "src/app.py", "type": "file",
                                 "required": True, "non_empty": True}],
             "expected_size": "small",
             "acceptance_criteria": ["src/app.py contains VALUE = 2"],
             "deterministic_checks": [], "skill": "extend"},
        ],
        "requirements_map": [], "completion_criteria": ["all checks pass"],
        "human_decisions": [],
    }
    t1 = architect_out["backlog"][0]
    callers = {"t1-extend": _caller(t1, [
        {"path": "src/app.py", "action": "write", "content": "VALUE = 2\n"}])}
    result, cycles = run_project_to_completion(sandbox, architect_out, callers)
    assert result["status"] == "complete", (result, cycles)
    assert dirty.read_text() == "user's own in-progress work\n"
    assert (sandbox["repo"] / "src" / "app.py").read_text() == "VALUE = 1\n"


# -- 7. intentional failing-test repair ------------------------------------------

def test_failing_test_repair_completes_natively(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = "auto"
    architect_out = {
        "architecture": "single module with a real bug to repair",
        "assumptions": [], "milestones": [{"id": "m1", "title": "core"}],
        "backlog": [
            {"id": "t1-fix", "milestone": "m1",
             "description": "implement add() and its passing test",
             "dependencies": [], "risk": "low", "security_relevant": False,
             "expected_paths": [
                 {"path": "mathlib.py", "type": "file", "required": True},
                 {"path": "tests/test_mathlib.py", "type": "file",
                  "required": True}],
             "expected_size": "small",
             "acceptance_criteria": ["add(2, 2) == 4"],
             "deterministic_checks": ["python -m pytest -q"],
             "skill": "fix"},
        ],
        "requirements_map": [], "completion_criteria": ["all checks pass"],
        "human_decisions": [],
    }
    t1 = architect_out["backlog"][0]
    broken = _worker([
        {"path": "mathlib.py", "action": "write",
         "content": "def add(a, b):\n    return a - b\n"},   # intentional bug
        {"path": "tests/test_mathlib.py", "action": "write",
         "content": "from mathlib import add\n\n\ndef test_add():\n"
                    "    assert add(2, 2) == 4\n"}])
    fixed = _worker([
        {"path": "mathlib.py", "action": "write",
         "content": "def add(a, b):\n    return a + b\n"},
        {"path": "tests/test_mathlib.py", "action": "write",
         "content": "from mathlib import add\n\n\ndef test_add():\n"
                    "    assert add(2, 2) == 4\n"}])
    callers = {"t1-fix": FakeCaller({
        "conductor": order_for(t1), "coder": [broken, fixed],
        "qa": qa_pass(), "security": sec_pass()})}
    result, cycles = run_project_to_completion(sandbox, architect_out, callers)
    assert result["status"] == "complete", (result, cycles)
    assert "mathlib.py" in [f for f in os.listdir(
        str(sandbox["agentic"] / "worktrees" / "project"))]


# -- 8. interrupted-cycle recovery -----------------------------------------------

def test_interrupted_cycle_recovery_completes_natively(sandbox):
    """Simulates a crash mid-project: a stale lock (holder process gone)
    plus an abandoned task worktree from the interrupted attempt. The
    next cycle must self-heal (Phase 1.G) and still reach completion."""
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = [
        {"name": "ok", "command": "python -c \"import sys; sys.exit(0)\"",
         "mandatory": True}]
    architect_out = {
        "architecture": "simple app", "assumptions": [],
        "milestones": [{"id": "m1", "title": "core"}],
        "backlog": [
            {"id": "t1-only", "milestone": "m1",
             "description": "set VALUE to 2", "dependencies": [],
             "risk": "low", "security_relevant": False,
             "expected_paths": [{"path": "src/app.py", "type": "file",
                                 "required": True}],
             "expected_size": "small", "acceptance_criteria": ["done"],
             "deterministic_checks": [], "skill": "app"},
        ],
        "requirements_map": [], "completion_criteria": ["all checks pass"],
        "human_decisions": [],
    }
    cfg = sandbox["cfg"]
    clock = Clock()
    plan = sandbox["repo"] / "plan.md"
    plan.write_text("# fixture\n", encoding="utf-8")
    start = project_start(cfg, str(plan),
                          caller=FakeCaller({"architect": architect_out}),
                          clock=clock)
    assert start["status"] == "started"
    a = str(sandbox["agentic"])

    # simulate the interruption: a project-lock file left behind by a
    # crashed process (handle released on exit, well past staleness).
    lock = projstate.ProjectLock(a)
    assert lock.acquire() is True
    os.close(lock.fd)
    lock.fd = None
    import datetime as _dt
    old = _dt.datetime.now().timestamp() - 7300
    os.utime(lock.path, (old, old))

    t1 = architect_out["backlog"][0]
    caller = _caller(t1, [{"path": "src/app.py", "action": "write",
                           "content": "VALUE = 2\n"}])
    r1 = run_cycle(cfg, caller=caller, clock=clock)
    assert r1["status"] == "success"
    clock.advance(minutes=31)
    r2 = run_cycle(cfg, caller=FakeCaller({"qa": qa_pass()}), clock=clock)
    assert r2["status"] == "complete"
    events = [l for l in (sandbox["agentic"] / "memory" / "decisions.jsonl"
                         ).read_text(encoding="utf-8").splitlines()]
    assert any("stale_lock_recovered" in e for e in events)


# -- 9. provider fallback simulation ---------------------------------------------

def test_provider_fallback_completes_natively(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = [
        {"name": "ok", "command": "python -c \"import sys; sys.exit(0)\"",
         "mandatory": True}]
    architect_out = {
        "architecture": "simple app", "assumptions": [],
        "milestones": [{"id": "m1", "title": "core"}],
        "backlog": [
            {"id": "t1-only", "milestone": "m1",
             "description": "set VALUE to 2", "dependencies": [],
             "risk": "low", "security_relevant": False,
             "expected_paths": [{"path": "src/app.py", "type": "file",
                                 "required": True}],
             "expected_size": "small", "acceptance_criteria": ["done"],
             "deterministic_checks": [], "skill": "app"},
        ],
        "requirements_map": [], "completion_criteria": ["all checks pass"],
        "human_decisions": [],
    }
    t1 = architect_out["backlog"][0]
    caller = FakeCaller({
        "conductor": order_for(t1),
        # primary backend exhausts usage mid-task; handoff to fallback
        "coder": [{"_error": "usage_limit", "backend": "mock",
                  "retry_after": 3600},
                 _worker([{"path": "src/app.py", "action": "write",
                          "content": "VALUE = 2\n"}])],
        "qa": qa_pass(), "security": sec_pass()})
    result, cycles = run_project_to_completion(sandbox, architect_out,
                                               {"t1-only": caller})
    assert result["status"] == "complete", (result, cycles)
    handoff_calls = [c for c in caller.calls if c["role"] == "coder"]
    assert len(handoff_calls) == 2
    assert handoff_calls[1]["chain"] == ["mock2"]


# -- 10. multi-project isolation --------------------------------------------------

def test_multi_project_isolation_completes_natively(tmp_path, base_cfg,
                                                     monkeypatch):
    """Two independently-configured projects, each with its own worktree
    tree and backlog, must never cross-contaminate: project B's files
    must never appear under project A's worktree or vice versa."""
    import copy
    import subprocess

    import core.config as config_mod

    def make_project(name):
        repo = tmp_path / name / "repo"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=str(repo),
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo),
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo),
                       check=True, capture_output=True)
        (repo / "README.md").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=str(repo),
                       check=True, capture_output=True)
        agentic = tmp_path / name / "agentic"
        import shutil
        from conftest import AGENTIC_SRC
        for sub in ("prompts", "schemas", "guardrails", "capabilities"):
            shutil.copytree(AGENTIC_SRC / sub, agentic / sub)
        for sub in ("memory", "queue", "runs", "goals", "worktrees"):
            (agentic / sub).mkdir(parents=True)
        cfg = copy.deepcopy(base_cfg)
        cfg["project"]["repository_root"] = str(repo)
        return {"repo": repo, "agentic": agentic, "cfg": project_cfg(
            {"cfg": cfg})}

    proj_a = make_project("proj-a")
    proj_b = make_project("proj-b")

    def architect_for(name):
        return {
            "architecture": name, "assumptions": [],
            "milestones": [{"id": "m1", "title": "core"}],
            "backlog": [
                {"id": "t1-%s" % name, "milestone": "m1",
                 "description": "write a marker file unique to %s" % name,
                 "dependencies": [], "risk": "low",
                 "security_relevant": False,
                 "expected_paths": [{"path": "marker-%s.txt" % name,
                                     "type": "file", "required": True}],
                 "expected_size": "small", "acceptance_criteria": ["done"],
                 "deterministic_checks": [], "skill": "marker"}],
            "requirements_map": [], "completion_criteria": ["all pass"],
            "human_decisions": [],
        }

    for proj, name in ((proj_a, "proj-a"), (proj_b, "proj-b")):
        monkeypatch.setattr(config_mod, "AGENTIC_DIR", proj["agentic"])
        proj["cfg"]["verification"]["commands"] = [
            {"name": "ok", "command": "python -c \"import sys; sys.exit(0)\"",
             "mandatory": True}]
        arch = architect_for(name)
        task = arch["backlog"][0]
        caller = _caller(task, [{"path": "marker-%s.txt" % name,
                                 "action": "write", "content": name}])
        result, _cycles = run_project_to_completion(proj, arch,
                                                     {task["id"]: caller})
        assert result["status"] == "complete", result

    worktree_a = proj_a["agentic"] / "worktrees" / "project"
    worktree_b = proj_b["agentic"] / "worktrees" / "project"
    assert (worktree_a / "marker-proj-a.txt").exists()
    assert not (worktree_a / "marker-proj-b.txt").exists()
    assert (worktree_b / "marker-proj-b.txt").exists()
    assert not (worktree_b / "marker-proj-a.txt").exists()


# -- 2. JavaScript application with Vitest ---------------------------------------

def test_javascript_vitest_app_completes_natively(sandbox):
    """The project SHAPE (package.json declaring a vitest test script,
    real source + test files) is fully real; the actual `npm run test`
    invocation is substituted with a safe, network-free command (real
    vitest execution needs `npm install`, which the default suite never
    does -- see module docstring)."""
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = [
        {"name": "npm-test-substitute",
         "command": "python -c \"import sys; sys.exit(0)\"",
         "mandatory": True, "kind": "test_suite"}]
    architect_out = {
        "architecture": "JavaScript app tested with Vitest", "assumptions": [],
        "milestones": [{"id": "m1", "title": "core"}],
        "backlog": [
            {"id": "t1-js", "milestone": "m1",
             "description": "implement add() with a package.json "
                            "declaring a vitest test script and a "
                            "real vitest test file",
             "dependencies": [], "risk": "low", "security_relevant": False,
             "expected_paths": [
                 {"path": "package.json", "type": "file", "required": True},
                 {"path": "src/add.js", "type": "file", "required": True},
                 {"path": "src/add.test.js", "type": "file",
                  "required": True}],
             "expected_size": "medium",
             "acceptance_criteria": ["add(2,2) === 4"],
             "deterministic_checks": ["npm run test"], "skill": "js"},
        ],
        "requirements_map": [], "completion_criteria": ["all checks pass"],
        "human_decisions": [],
    }
    t1 = architect_out["backlog"][0]
    callers = {"t1-js": _caller(t1, [
        {"path": "package.json", "action": "write",
         "content": '{"name": "app", "scripts": {"test": "vitest run"}, '
                    '"devDependencies": {"vitest": "^1.0.0"}}\n'},
        {"path": "src/add.js", "action": "write",
         "content": "export function add(a, b) {\n  return a + b;\n}\n"},
        {"path": "src/add.test.js", "action": "write",
         "content": "import { expect, test } from 'vitest';\n"
                    "import { add } from './add.js';\n\n"
                    "test('adds', () => { expect(add(2, 2)).toBe(4); });\n"},
    ])}
    result, cycles = run_project_to_completion(sandbox, architect_out, callers)
    assert result["status"] == "complete", (result, cycles)
    worktree = sandbox["agentic"] / "worktrees" / "project"
    assert (worktree / "package.json").exists()
    assert "vitest" in (worktree / "package.json").read_text(encoding="utf-8")


# -- 4. FastAPI service (real fastapi + real pytest) -----------------------------

def test_fastapi_service_completes_natively(sandbox):
    """fastapi is actually installed in this environment (the dashboard
    uses it) -- so this fixture is fully real: a genuine FastAPI app and
    a genuine `TestClient`-based pytest test, both really executed."""
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = "auto"
    architect_out = {
        "architecture": "FastAPI service", "assumptions": [],
        "milestones": [{"id": "m1", "title": "core"}],
        "backlog": [
            {"id": "t1-api", "milestone": "m1",
             "description": "implement a FastAPI health endpoint and "
                            "its test", "dependencies": [], "risk": "low",
             "security_relevant": False,
             "expected_paths": [
                 {"path": "app.py", "type": "file", "required": True},
                 {"path": "tests/test_app.py", "type": "file",
                  "required": True}],
             "expected_size": "medium",
             "acceptance_criteria": ["GET /health returns 200"],
             "deterministic_checks": ["python -m pytest -q"],
             "skill": "api"},
        ],
        "requirements_map": [], "completion_criteria": ["all checks pass"],
        "human_decisions": [],
    }
    t1 = architect_out["backlog"][0]
    callers = {"t1-api": _caller(t1, [
        {"path": "app.py", "action": "write",
         "content": "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
                    "@app.get('/health')\ndef health():\n"
                    "    return {'status': 'ok'}\n"},
        {"path": "tests/test_app.py", "action": "write",
         "content": "from fastapi.testclient import TestClient\n"
                    "from app import app\n\nclient = TestClient(app)\n\n\n"
                    "def test_health():\n"
                    "    response = client.get('/health')\n"
                    "    assert response.status_code == 200\n"
                    "    assert response.json() == {'status': 'ok'}\n"}])}
    result, cycles = run_project_to_completion(sandbox, architect_out, callers)
    assert result["status"] == "complete", (result, cycles)


# -- 5. React application ----------------------------------------------------------

def test_react_app_completes_natively(sandbox):
    """Same safe-substitute rationale as the Vitest fixture: the project
    SHAPE (package.json + JSX component) is real, the build/test command
    is substituted to stay network-free."""
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = [
        {"name": "react-build-substitute",
         "command": "python -c \"import sys; sys.exit(0)\"",
         "mandatory": True, "kind": "build"}]
    architect_out = {
        "architecture": "React application", "assumptions": [],
        "milestones": [{"id": "m1", "title": "core"}],
        "backlog": [
            {"id": "t1-react", "milestone": "m1",
             "description": "scaffold a React app with a Counter "
                            "component", "dependencies": [], "risk": "low",
             "security_relevant": False,
             "expected_paths": [
                 {"path": "package.json", "type": "file", "required": True},
                 {"path": "src/Counter.jsx", "type": "file",
                  "required": True, "non_empty": True}],
             "expected_size": "medium",
             "acceptance_criteria": ["Counter renders a button"],
             "deterministic_checks": ["npm run build"], "skill": "react"},
        ],
        "requirements_map": [], "completion_criteria": ["all checks pass"],
        "human_decisions": [],
    }
    t1 = architect_out["backlog"][0]
    callers = {"t1-react": _caller(t1, [
        {"path": "package.json", "action": "write",
         "content": '{"name": "react-app", "dependencies": '
                    '{"react": "^18.0.0"}}\n'},
        {"path": "src/Counter.jsx", "action": "write",
         "content": "import React, { useState } from 'react';\n\n"
                    "export default function Counter() {\n"
                    "  const [n, setN] = useState(0);\n"
                    "  return <button onClick={() => setN(n + 1)}>"
                    "{n}</button>;\n}\n"}])}
    result, cycles = run_project_to_completion(sandbox, architect_out, callers)
    assert result["status"] == "complete", (result, cycles)
    worktree = sandbox["agentic"] / "worktrees" / "project"
    assert (worktree / "src" / "Counter.jsx").exists()
