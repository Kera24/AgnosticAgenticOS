"""Phase 11 — the mocked full-project scenario, end to end through the REAL
call surface (make_caller -> Context Broker -> routing -> scripted backend
adapters). No real provider is ever contacted.

Covered scenario (spec steps 1–21): plan -> architect -> backlog + index ->
cycle: broker package (+ skill selection) -> worker -> deterministic failure
-> repair -> QA rejection -> repair -> approval -> local commit -> memory +
knowledge + incremental index -> persisted cooling -> next cycle -> final
audit -> completion notification -> no automatic restart.
"""
import datetime
import json
import os

import pytest

from conftest import Clock, project_cfg
from core import errors, project as project_mod, projstate
from core.context.ledger import read_packages
from core.knowledge import KnowledgeVault
from core.memsvc import MemoryService
from core.scheduler import Scheduler


class ScriptedAdapter:
    """Backend adapter scripted per role. Sequential outputs; the last one
    repeats. Records every (role, prompt)."""
    backend_type = "api"

    def __init__(self, name, script, calls):
        self.name = name
        self.script = script
        self.calls = calls

    def invoke(self, role, prompt, input_data, workspace, permissions,
               timeout):
        self.calls.append({"backend": self.name, "role": role,
                           "prompt": prompt, "workspace": workspace})
        outputs = self.script.get(role)
        assert outputs, "no scripted output for role %r" % role
        item = outputs.pop(0) if len(outputs) > 1 else outputs[0]
        if isinstance(item, Exception):
            raise item
        return {"ok": True, "backend": self.name, "backend_type": "api",
                "model": "scripted", "role": role, "provider": self.name,
                "content": json.dumps(item), "structured_output": {},
                "usage": {"input_tokens": 500, "cached_input_tokens": 0,
                          "output_tokens": 200, "reasoning_tokens": None,
                          "estimated": False},
                "capacity": {"remaining_reported": None, "reset_at": None,
                             "retry_after_seconds": None},
                "finish_reason": "completed", "refusal": False,
                "exit_code": 0, "estimated_cost_usd": 0.0, "error": None}


def architect_out():
    def task(tid, description, paths):
        return {"id": tid, "milestone": "m1", "description": description,
                "dependencies": [] if tid == "t1-value" else ["t1-value"],
                "risk": "low", "security_relevant": False,
                "expected_paths": paths, "expected_size": "small",
                "acceptance_criteria": ["deterministic check passes"],
                "deterministic_checks": [], "skill": "app-code"}
    return {"architecture": "single module app",
            "assumptions": ["python available"],
            "milestones": [{"id": "m1", "title": "core"}],
            "backlog": [task("t1-value", "set VALUE to 2 with tests",
                             ["src/**"]),
                        task("t2-readme", "document the value constant",
                             ["src/**"])],
            "requirements_map": [{"requirement": "value is 2",
                                  "tasks": ["t1-value"]}],
            "completion_criteria": ["VALUE equals 2"],
            "human_decisions": []}


def order_for(task_id, description):
    return {"action": "execute", "item": description, "skill": "app-code",
            "spec": description,
            "done_when": [{"id": "DW-1",
                           "condition": "check passes", "command": None}],
            "allowed_paths": ["src/**"], "forbidden_paths": [],
            "maximum_changed_lines": 50, "risk": "low",
            "queue_reason": None,
            "acceptance_criteria": ["deterministic check passes"]}


def edit(content, path="src/app.py"):
    return {"summary": "edit", "blocked": False, "blocker": None,
            "edits": [{"path": path, "action": "write",
                       "content": content}], "commands": []}


def qa(verdict, repairs=None):
    return {"verdict": verdict,
            "done_when_results": [{"id": "DW-1",
                                   "passed": verdict == "pass",
                                   "evidence": ["diff"]}],
            "out_of_scope_changes": [],
            "test_integrity_preserved": True,
            "reason": "reviewed",
            "required_repairs": repairs or []}


@pytest.fixture
def world(sandbox, monkeypatch, tmp_path):
    """Real orchestration + broker + routing over scripted adapters."""
    cfg = project_cfg(sandbox)
    # deterministic check: passes only when src/app.py says VALUE = 2
    cfg["verification"]["commands"] = [{
        "name": "value-check",
        "command": ("python -c \"import sys; "
                    "sys.exit(0 if open('src/app.py').read()"
                    ".startswith('VALUE = 2') else 1)\""),
        "mandatory": True}]
    cfg["notifications"] = {"desktop": False}
    calls = []
    script = {
        "architect": [architect_out()],
        "conductor": [order_for("t1-value", "set VALUE to 2"),
                      order_for("t2-readme", "document the constant")],
        "coder": [edit("VALUE = 3\n"),          # deterministic failure
                  edit("VALUE = 2\n"),          # repair passes the gate
                  edit("VALUE = 2  # documented\n"),   # QA-requested repair
                  edit("VALUE = 2  # documented\n")],  # task 2
        "qa": [qa("fail", ["add the documentation comment"]),  # rejection
               qa("pass"), qa("pass"), qa("pass")],
        "security": [qa("pass")],
    }
    monkeypatch.setattr(
        project_mod.backends, "build_backend",
        lambda cfg_, name, **kw: ScriptedAdapter(name, script, calls))
    clock = Clock()
    return {"cfg": cfg, "sandbox": sandbox, "calls": calls, "clock": clock,
            "script": script, "memdir": str(sandbox["agentic"] / "memory")}


def run_cycle(world):
    return project_mod.run_cycle(world["cfg"], clock=world["clock"])


def test_full_autonomous_build(world):
    cfg, sandbox, clock = world["cfg"], world["sandbox"], world["clock"]
    a = str(sandbox["agentic"])
    memdir = world["memdir"]

    # 1–3: plan -> architect -> backlog + code index
    plan = sandbox["repo"] / "plan.md"
    plan.write_text("# Plan\n\nBuild an app where VALUE equals 2.\n" * 3,
                    encoding="utf-8")
    started = project_mod.project_start(cfg, str(plan), clock=clock)
    assert started["status"] == "started" and started["tasks"] == 2
    assert os.path.exists(os.path.join(memdir, "code-index", "state.json"))

    # 4–13: one cycle with deterministic failure -> repair -> QA reject ->
    # repair -> approval -> local commit
    result = run_cycle(world)
    assert result["status"] == "success"
    coder_prompts = [c for c in world["calls"] if c["role"] == "coder"]
    assert len(coder_prompts) == 3
    # 5: every prompt was broker-built (policy header + ledger record)
    assert all("# OS POLICY" in c["prompt"] for c in world["calls"])
    packages = read_packages(memdir, limit=1000)
    assert len(packages) == len(world["calls"])
    # repair packets carried structured feedback, not reviewer transcripts
    assert "value-check" in coder_prompts[1]["prompt"]
    assert "add the documentation comment" in coder_prompts[2]["prompt"]
    # 13: the cycle committed locally on the project branch
    worktree = os.path.join(a, "worktrees", "project")
    from core import gitops
    log = gitops.run_git(["log", "--oneline", "-3"], cwd=worktree)
    assert "t1-value" in log

    # 14: memory + knowledge updated deterministically
    memory = MemoryService(memdir, "test")
    types = {r["type"] for r in memory.search(limit=100)}
    assert {"cycle_outcome", "reviewer_finding"} <= types
    vault = KnowledgeVault(cfg, a)
    assert "current-state.md" in vault.documents()

    # 16: cooling persisted, never slept
    scheduler = Scheduler(cfg, memdir, clock=clock)
    assert scheduler.state["state"] == "cooling"
    assert run_cycle(world)["status"] == "not_eligible"

    # 17–18: cooling elapses (fresh process), remaining work completes
    clock.advance(minutes=31)
    result2 = run_cycle(world)
    assert result2["status"] == "success"
    assert projstate.refresh_progress(a)["backlog_complete"]

    # 19–20: final audit passes and the user is notified once
    clock.advance(minutes=31)
    final = run_cycle(world)          # empty backlog routes to final audit
    assert final["status"] == "complete"
    inbox = open(os.path.join(memdir, "notifications.log"),
                 encoding="utf-8").read()
    assert "project_complete" in inbox

    # 21: a completed project never restarts automatically
    clock.advance(minutes=120)
    assert run_cycle(world)["status"] == "not_eligible"
    assert Scheduler(cfg, memdir, clock=clock).state["state"] == "complete"


def test_auth_failure_stops_without_fallback(world):
    cfg, sandbox = world["cfg"], world["sandbox"]
    plan = sandbox["repo"] / "plan.md"
    plan.write_text("# Plan\n\nBuild an app where VALUE equals 2.\n",
                    encoding="utf-8")
    world["script"]["conductor"] = [
        errors.AuthError("login required", provider="mock")]
    project_mod.project_start(cfg, str(plan), clock=world["clock"])
    result = run_cycle(world)
    assert result["status"] == "failure"
    # the auth failure never reached the fallback backend
    conductor_backends = {c["backend"] for c in world["calls"]
                          if c["role"] == "conductor"}
    assert conductor_backends == {"mock"}


def test_restart_during_work_recovers_via_stale_lock(world, monkeypatch):
    cfg, sandbox = world["cfg"], world["sandbox"]
    a = str(sandbox["agentic"])
    plan = sandbox["repo"] / "plan.md"
    plan.write_text("# Plan\n\nBuild an app where VALUE equals 2.\n",
                    encoding="utf-8")
    project_mod.project_start(cfg, str(plan), clock=world["clock"])
    # simulate a crash mid-cycle: the lock file was left behind
    lock_path = os.path.join(a, "project", "project.lock")
    with open(lock_path, "w") as fh:
        fh.write("99999")
    assert run_cycle(world)["status"] == "locked"
    # after the stale window the lock is broken and work resumes
    old = world["clock"].now.timestamp() - 8000
    os.utime(lock_path, (old, old))
    assert run_cycle(world)["status"] == "success"


def test_context_budget_overflow_fails_loudly(world):
    cfg, sandbox = world["cfg"], world["sandbox"]
    cfg["context"] = {"default_input_budget_tokens": 200,
                      "reserved_output_tokens": 50}
    plan = sandbox["repo"] / "plan.md"
    plan.write_text("# Plan\n\n" + "requirement line\n" * 400,
                    encoding="utf-8")
    result = project_mod.project_start(cfg, str(plan), clock=world["clock"])
    # the oversized mandatory plan is refused, never silently truncated
    assert result["status"] == "architect_failed"
    assert world["calls"] == []          # nothing was ever sent


def test_failing_final_audit_reported(world):
    cfg, sandbox = world["cfg"], world["sandbox"]
    a = str(sandbox["agentic"])
    from conftest import seed_project, simple_task
    seed_project(sandbox, [simple_task(tid="t1-first", status="done")])
    # final QA verdict fails -> audit_failed, project not marked complete
    world["script"]["qa"] = [qa("fail")]
    result = project_mod.final_audit(cfg, caller=None, clock=world["clock"])
    assert result["status"] == "audit_failed"
    assert "final_independent_review" in result["failed_checks"]
    assert Scheduler(cfg, world["memdir"],
                     clock=world["clock"]).state["project_status"] \
        == "audit_failed"
