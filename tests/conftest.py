import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
AGENTIC_SRC = REPO / ".agentic"
sys.path.insert(0, str(AGENTIC_SRC))

from core import errors  # noqa: E402


def oai_body(content, in_tok=10, out_tok=5, finish="stop", model="mock-model",
             refusal=None):
    msg = {"role": "assistant", "content": content}
    if refusal is not None:
        msg["refusal"] = refusal
    return json.dumps({
        "choices": [{"message": msg, "finish_reason": finish}],
        "model": model,
        "usage": {"prompt_tokens": in_tok, "completion_tokens": out_tok,
                  "prompt_tokens_details": {"cached_tokens": 0}},
    })


class Transport:
    """Scripted fake transport: pops one queued (status, body) or exception
    per call and records everything sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers,
                           "body": json.loads(body.decode("utf-8")),
                           "timeout": timeout})
        item = self.responses.pop(0) if self.responses else self.responses_exhausted()
        if isinstance(item, Exception):
            raise item
        return item

    @staticmethod
    def responses_exhausted():
        raise AssertionError("fake transport ran out of scripted responses")


@pytest.fixture
def base_cfg():
    return copy.deepcopy({
        "version": 1,
        "project": {"name": "test", "repository_root": ".."},
        "execution": {"mode": "review", "max_tasks_per_run": 1,
                      "max_changed_lines": 400, "max_changed_files": 20,
                      "worktree_enabled": True,
                      "command_timeout_seconds": 60,
                      "goal_timeout_seconds": 10, "safe_commands": []},
        "roles": {
            "triage": {"provider": "mock", "model": "triage-model",
                       "temperature": 0, "max_output_tokens": 500, "tools": []},
            "conductor": {"provider": "mock", "model": "conductor-model",
                          "temperature": 0, "max_output_tokens": 500,
                          "tools": []},
            "worker": {"provider": "mock", "model": "worker-model",
                       "temperature": 0, "max_output_tokens": 500, "tools": []},
            "verifier": {"provider": "mock", "model": "verifier-model",
                         "temperature": 0, "max_output_tokens": 500,
                         "tools": []},
        },
        "providers": {
            "mock": {"type": "openai_compatible",
                     "base_url": "http://mock.local/v1",
                     "api_key_required": False, "cost_free": True},
        },
        "budget": {"daily_limit_usd": 5, "per_run_limit_usd": 2,
                   "max_input_tokens_per_run": 500000,
                   "max_output_tokens_per_run": 50000,
                   "unknown_price_policy": "block", "warning_percentage": 80},
        "pricing": {},
        "retry": {"maximum_attempts_per_provider": 2,
                  "backoff_seconds": [0, 0], "allow_fallback": True,
                  "fallback_on_refusal": False},
        "verification": {"commands": [
            {"name": "ok-check",
             "command": "python -c \"import sys; sys.exit(0)\"",
             "mandatory": True}], "fail_fast": True},
        "trust": {"sensitive_auto_allowed": [], "sensitive_skills": [],
                  "track_by_model": False},
        "contract": {"extra_protected_paths": []},
        "integrations": {"github_cli": "off"},
    })


@pytest.fixture
def budget(base_cfg, tmp_path):
    from core.budget import Budget
    return Budget(base_cfg, str(tmp_path / "memory"), "test-run")


def git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.fixture
def sandbox(tmp_path, base_cfg, monkeypatch):
    """Isolated environment: a real throwaway git repo plus a sandboxed
    .agentic dir (prompts/schemas/guardrails copied from the source tree)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(["init", "-b", "main"], repo)
    git(["config", "user.email", "t@t"], repo)
    git(["config", "user.name", "t"], repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests_placeholder.txt").write_text("keep\n", encoding="utf-8")
    git(["add", "-A"], repo)
    git(["commit", "-m", "initial"], repo)

    agentic = tmp_path / "agentic"
    for sub in ("prompts", "schemas", "guardrails", "capabilities"):
        shutil.copytree(AGENTIC_SRC / sub, agentic / sub)
    for sub in ("memory", "queue", "runs", "goals", "worktrees"):
        (agentic / sub).mkdir()

    import core.config as config_mod
    monkeypatch.setattr(config_mod, "AGENTIC_DIR", agentic)
    cfg = copy.deepcopy(base_cfg)
    cfg["project"]["repository_root"] = str(repo)
    return {"repo": repo, "agentic": agentic, "cfg": cfg}


class FakeInvoker:
    """Stands in for invoke_model inside run_tick: returns scripted
    structured outputs per role and records every call."""

    def __init__(self, by_role):
        self.by_role = {k: list(v) if isinstance(v, list) else [v]
                        for k, v in by_role.items()}
        self.calls = []

    def __call__(self, role, prompt, input_data, output_schema, budget):
        self.calls.append({"role": role, "input": input_data,
                           "prompt": prompt})
        outputs = self.by_role.get(role)
        assert outputs, "no scripted output for role %r" % role
        structured = outputs.pop(0) if len(outputs) > 1 else outputs[0]
        if isinstance(structured, Exception):
            raise structured
        if isinstance(structured, dict) and structured.get("_raw_response"):
            return structured["_raw_response"]
        return {"ok": True, "provider": "mock", "model": "mock-model",
                "content": json.dumps(structured),
                "structured_output": structured,
                "usage": {"input_tokens": 1, "output_tokens": 1,
                          "cached_tokens": 0},
                "estimated_cost_usd": 0.0, "finish_reason": "stop",
                "refusal": False, "error": None}


class FakeRunner:
    """Scripted CLI process runner: pops one result dict per call, records
    every argv. Result template: exit_code/stdout/stderr/timed_out."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, cwd=None, timeout=120, stdin_text=None):
        self.calls.append({"argv": list(argv), "cwd": cwd,
                           "timeout": timeout, "stdin": stdin_text})
        item = self.responses.pop(0) if self.responses else \
            {"exit_code": 0, "stdout": "", "stderr": ""}
        if isinstance(item, Exception):
            raise item
        result = {"exit_code": 0, "stdout": "", "stderr": "",
                  "timed_out": False, "argv": list(argv), "cwd": cwd,
                  "shell": False, "source": "config",
                  "duration_seconds": 0.01}
        result.update(item)
        return result


class Clock:
    """Controllable clock for scheduler/breaker/capacity tests."""

    def __init__(self):
        import datetime
        self.now = datetime.datetime(2026, 7, 15, 12, 0, 0)

    def __call__(self):
        return self.now

    def advance(self, minutes=0, seconds=0):
        import datetime
        self.now += datetime.timedelta(minutes=minutes, seconds=seconds)


class FakeCaller:
    """Stands in for the common backend caller in project-mode tests."""

    def __init__(self, by_role):
        self.by_role = {k: list(v) if isinstance(v, list) else [v]
                        for k, v in by_role.items()}
        self.calls = []

    def __call__(self, role, prompt, input_data=None, schema=None,
                 workspace=None, permissions="read", timeout=None,
                 chain=None):
        self.calls.append({"role": role, "input": input_data,
                           "workspace": workspace,
                           "permissions": permissions, "chain": chain})
        outputs = self.by_role.get(role)
        assert outputs, "no scripted output for role %r" % role
        item = outputs.pop(0) if len(outputs) > 1 else outputs[0]
        if callable(item):
            item = item(workspace, input_data)
        if isinstance(item, dict) and item.get("_error"):
            return {"ok": False, "backend": item.get("backend", "mock"),
                    "backend_type": "api", "model": None, "role": role,
                    "provider": "mock", "content": "",
                    "structured_output": {},
                    "usage": {"input_tokens": None,
                              "cached_input_tokens": None,
                              "output_tokens": None,
                              "reasoning_tokens": None, "estimated": True},
                    "capacity": {"remaining_reported": None, "reset_at": None,
                                 "retry_after_seconds":
                                     item.get("retry_after")},
                    "finish_reason": "error", "refusal": False,
                    "exit_code": None, "estimated_cost_usd": 0.0,
                    "error": {"kind": item["_error"],
                              "detail": item.get("detail", "")}}
        content = item.get("_content") if isinstance(item, dict) else None
        structured = {} if content is not None else item
        return {"ok": True, "backend": item.get("_backend", "mock")
                if isinstance(item, dict) else "mock",
                "backend_type": "api", "model": "mock-model", "role": role,
                "provider": "mock",
                "content": content if content is not None
                else json.dumps(structured),
                "structured_output": structured,
                "usage": {"input_tokens": 100, "cached_input_tokens": 0,
                          "output_tokens": 50, "reasoning_tokens": None,
                          "estimated": False},
                "capacity": {"remaining_reported": None, "reset_at": None,
                             "retry_after_seconds": None},
                "finish_reason": "completed", "refusal": False,
                "exit_code": 0, "estimated_cost_usd": 0.0, "error": None}


def project_cfg(sandbox):
    """Extend the sandbox cfg with full-project-build configuration."""
    cfg = sandbox["cfg"]
    cfg["backends"] = {
        "mock": {"type": "api", "provider": "mock", "model": "mock-model"},
        "mock2": {"type": "api", "provider": "mock", "model": "mock-2"},
    }
    cfg["routing"] = {"mode": "simple", "primary": "mock",
                      "fallbacks": ["mock2"]}
    cfg["repair"] = {"maximum_attempts_per_task": 3,
                     "maximum_replans_per_task": 2}
    cfg["capacity"] = {"safety_multiplier": 1.35}
    cfg["interaction"] = {"mode": "completion_only"}
    cfg["notifications"] = {"desktop": False}
    cfg["limits"] = {}
    cfg["scheduler"] = {"cooling": {"after_success_minutes": 30,
                                    "after_failure_minutes": 30,
                                    "minimum_minutes": 5,
                                    "maximum_minutes": 360},
                        "continuation": {"automatic": True},
                        "operating_window": {"enabled": False}}
    return cfg


def seed_project(sandbox, tasks, milestones=None):
    import core.projstate as projstate
    a = str(sandbox["agentic"])
    milestones = milestones or [{"id": "m1", "title": "milestone 1"}]
    projstate.write_yaml(a, "milestones.yaml", {"milestones": milestones})
    projstate.save_backlog(a, [projstate.normalize_task(t) for t in tasks])
    projstate.write_yaml(a, "acceptance-criteria.yaml",
                         {"requirements_map": [],
                          "completion_criteria": ["all checks pass"]})
    projstate.write_yaml(a, "decisions.yaml", {"human_decisions_needed": [],
                                               "decided": []})
    projstate.write_yaml(a, "blockers.yaml", {"blockers": []})
    projstate.write_text(a, "PROJECT.md", "# plan")
    projstate.write_text(a, "architecture.md", "# arch")
    projstate.refresh_progress(a)


def simple_task(tid="t1-first", milestone="m1", **over):
    task = {"id": tid, "milestone": milestone,
            "description": "set VALUE to 2 in src/app.py",
            "dependencies": [], "risk": "low", "security_relevant": False,
            "expected_paths": ["src/**"], "expected_size": "small",
            "acceptance_criteria": ["src/app.py contains VALUE = 2"],
            "deterministic_checks": [], "skill": "app-code"}
    task.update(over)
    return task


def proj_order(task, **over):
    order = order_out(item=task["description"], skill=task.get("skill")
                      or task["id"], allowed_paths=task["expected_paths"])
    order.update(over)
    return order


def triage_out(sensitive=False, actionable=True):
    return {"status": "findings", "findings": [{
        "finding": "lint error in src/app.py",
        "evidence": ["src/app.py"],
        "status": "actionable" if actionable else "informational",
        "contract_sensitive": sensitive, "confidence": 0.9}]}


def order_out(**over):
    order = {"action": "execute", "item": "fix lint in src/app.py",
             "skill": "fix-lint-debt", "spec": "change VALUE to 2",
             "done_when": [{"id": "DW-1",
                            "condition": "src/app.py contains VALUE = 2",
                            "command": None}],
             "allowed_paths": ["src/app.py"], "forbidden_paths": [],
             "maximum_changed_lines": 10, "risk": "low", "queue_reason": None}
    order.update(over)
    return order


def worker_out(**over):
    out = {"summary": "changed VALUE to 2", "blocked": False, "blocker": None,
           "edits": [{"path": "src/app.py", "action": "write",
                      "content": "VALUE = 2\n"}], "commands": []}
    out.update(over)
    return out


def verifier_out(verdict="pass", integrity=True):
    return {"verdict": verdict,
            "done_when_results": [{"id": "DW-1", "passed": verdict == "pass",
                                   "evidence": ["src/app.py diff"]}],
            "out_of_scope_changes": [], "test_integrity_preserved": integrity,
            "reason": "checked the diff"}
