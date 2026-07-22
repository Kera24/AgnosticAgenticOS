"""Central model-resolution policy (core.modelres): the single
resolve_model() used by every backend adapter (Codex, Claude/Qwen-style
configured CLIs, Ollama, API adapters) AND by doctor's readiness report.

Covers the confirmed bug -- `project start ... --primary codex --fallback
ollama` failing with `model_unavailable` even though Codex is installed,
authenticated, and smoke-tested -- end to end, with mocked subprocesses
and HTTP transports only. No real Codex, Claude, or Ollama call is ever
made here.
"""
import copy
import json

import pytest

from conftest import FakeRunner, OllamaStream, Transport, oai_body, ollama_event
from core import errors
from core.modelres import is_embedding_model, is_placeholder_model, resolve_model

# -- pure predicate tests --------------------------------------------------------------

PLACEHOLDER_VALUES = [
    None, "", "auto", "AUTO", "example-reasoning-model",
    "example/small-triage-model", "example-coding-model",
    "example-local-model", "example-verification-model",
    "example-fallback-model", "example-anything-new",
    "example/anything-new", "configurable-model",
]


@pytest.mark.parametrize("value", PLACEHOLDER_VALUES)
def test_is_placeholder_model_recognises_all_known_forms(value):
    assert is_placeholder_model(value)


@pytest.mark.parametrize("value", [
    "gpt-5-codex", "claude-opus-4-8", "qwen3.5:latest", "gpt-4-turbo",
    "mock-large-v1",
])
def test_is_placeholder_model_false_for_real_models(value):
    assert not is_placeholder_model(value)


def test_is_embedding_model_recognises_known_families():
    assert is_embedding_model("nomic-embed-text-v2-moe:latest")
    assert is_embedding_model("bge-large")
    assert is_embedding_model("text-embedding-3-small")
    assert not is_embedding_model("qwen3.5:latest")
    assert not is_embedding_model(None)


# -- resolve_model() unit tests ---------------------------------------------------------

def test_resolve_model_cli_never_sends_role_model_to_codex():
    """The confirmed bug shape: an API-oriented role placeholder must
    never reach a CLI backend's model resolution -- only the backend's
    OWN configured model is consulted."""
    resolution = resolve_model(
        "conductor", "cli", "codex",
        role_model="example-reasoning-model", backend_model=None)
    assert resolution["model_flag_emitted"] is False
    assert resolution["resolved_model"] == "provider_default"
    assert resolution["model_source"] == "cli_provider_default"
    assert resolution["valid"] is True
    assert resolution["role_model"] == "example-reasoning-model"
    assert resolution["configured_model"] is None


def test_resolve_model_local_prefers_explicit_over_default():
    resolution = resolve_model(
        "coder", "local", "ollama", backend_model="qwen3.5:latest",
        detected_models=["qwen3.5:latest", "llama3:8b"])
    assert resolution["resolved_model"] == "qwen3.5:latest"
    assert resolution["model_source"] == "explicit_config"
    assert resolution["valid"] is True


def test_resolve_model_local_falls_back_to_first_generation_model():
    resolution = resolve_model(
        "coder", "local", "ollama", backend_model=None,
        detected_models=["nomic-embed-text-v2-moe:latest", "llama3:8b"])
    assert resolution["resolved_model"] == "llama3:8b"
    assert resolution["model_source"] == "local_default"
    assert resolution["valid"] is True


def test_resolve_model_local_rejects_typo(monkeypatch=None):
    resolution = resolve_model(
        "architect", "local", "ollama", backend_model="qwew3.5:latest",
        detected_models=["qwen3.5:latest"])
    assert resolution["valid"] is False
    assert "qwew3.5:latest" in resolution["explanation"]


def test_resolve_model_api_never_substitutes_arbitrary_model():
    resolution = resolve_model("qa", "api", "openai_api", backend_model=None)
    assert resolution["valid"] is False
    assert resolution["resolved_model"] is None
    assert resolution["model_source"] == "api_required"


def test_diagnostic_lines_never_include_role_model_placeholder_content():
    from core.modelres import diagnostic_lines
    resolution = resolve_model(
        "conductor", "cli", "codex",
        role_model="example-reasoning-model", backend_model=None)
    lines = diagnostic_lines(resolution)
    joined = "\n".join(lines)
    assert "role=conductor" in joined
    assert "backend=codex" in joined
    assert "resolved_model=provider_default" in joined
    assert "model_flag_emitted=false" in joined


# -- doctor / invocation consistency (#17) -----------------------------------------------

def test_doctor_and_invocation_resolve_codex_identically(tmp_path):
    """Doctor's role->backend->model resolution for "architect" must come
    from the exact same resolve_model() call the real Codex adapter makes
    -- this is the fix for the contradiction where doctor said READY while
    the first real call failed with model_unavailable."""
    from core import doctor as doctor_mod
    from core.backends import build_backend
    from core.breaker import BreakerBoard
    from core.capacity import CapacityLedger

    cfg = {
        "roles": {},
        "backends": {
            "codex": {"type": "cli", "kind": "codex", "binary": "codex"},
            "ollama": {"type": "local", "model": "qwen3.5:latest"},
        },
        "routing": {"mode": "simple", "primary": "codex",
                   "fallbacks": ["ollama"]},
    }
    which = lambda b: "C:/bin/%s" % b   # noqa: E731
    adapter = build_backend(cfg, "codex", runner=FakeRunner([]), which=which)
    adapter.build_argv("architect", "read", "C:/ws")
    invocation = adapter.last_model_resolution

    memory = str(tmp_path / "memory")
    board, ledger = BreakerBoard(memory), CapacityLedger(cfg, memory)
    detected = {"codex": {"models": []},
               "ollama": {"models": ["qwen3.5:latest"]}}
    lines = []
    project_ready = doctor_mod._project_model_resolution(
        cfg, lambda level, msg: lines.append((level, msg)), board, ledger,
        detected, memory)

    architect_line = next(msg for _, msg in lines
                          if msg.startswith("role architect"))
    assert project_ready is True
    assert ("model %s" % invocation["resolved_model"]) in architect_line
    assert ("source=%s" % invocation["model_source"]) in architect_line
    assert all(level == "ok" for level, msg in lines
              if msg.startswith(("role ", "fallback ")))


def test_doctor_reports_not_ready_for_api_backend_placeholder(tmp_path):
    """Expected behaviour table: API backend + placeholder -> NOT READY,
    explicit API model required."""
    from core import doctor as doctor_mod
    from core.breaker import BreakerBoard
    from core.capacity import CapacityLedger

    cfg = {
        "roles": {},
        "backends": {"openai_api": {"type": "api", "provider": "openai",
                                    "model": "example-reasoning-model"}},
        "routing": {"mode": "simple", "primary": "openai_api",
                   "fallbacks": []},
    }
    memory = str(tmp_path / "memory")
    board, ledger = BreakerBoard(memory), CapacityLedger(cfg, memory)
    lines = []
    project_ready = doctor_mod._project_model_resolution(
        cfg, lambda level, msg: lines.append((level, msg)), board, ledger,
        {}, memory)
    assert project_ready is False
    architect_line = next(msg for lvl, msg in lines
                          if msg.startswith("role architect"))
    assert "explicit" in architect_line.lower()


# -- setup writes model-neutral CLI configuration (#18) ---------------------------------

def test_setup_writes_model_neutral_cli_configuration(base_cfg, tmp_path,
                                                       monkeypatch):
    import core.config as config_mod
    from core.setupwiz import run_setup
    agentic = tmp_path / "agentic"
    agentic.mkdir()
    monkeypatch.setattr(config_mod, "AGENTIC_DIR", agentic)

    which = lambda b: {"codex": "C:/bin/codex"}.get(b)   # noqa: E731
    runner = FakeRunner([
        {"stdout": "codex-cli 1.2.3"},          # codex --version
        {"stdout": "Logged in using ChatGPT"},  # codex login status
    ])
    result = run_setup(cfg=base_cfg, runner=runner, which=which, smoke=False,
                       answers=["simple", "codex", "", "20", "30",
                               "completion_only", "always", "no"])
    assert result["ok"]
    machine = result["machine"]
    assert machine["backends"]["codex"]["model"] == "auto"
    assert is_placeholder_model(machine["backends"]["codex"]["model"])
    # setup never asked for (or wrote) an API-style model name for codex
    text = "\n".join(result["output"])
    assert "example-" not in text


# -- mocked end-to-end: the exact confirmed-failing command --------------------------------

ARCHITECT_PAYLOAD = {
    "architecture": "single-service app", "assumptions": [],
    "milestones": [{"id": "m1", "title": "core"}],
    "backlog": [{"id": "t1-core", "milestone": "m1",
                "description": "implement the core feature",
                "dependencies": [], "risk": "low",
                "security_relevant": False, "expected_paths": ["src/**"],
                "expected_size": "small",
                "acceptance_criteria": ["check passes"],
                "deterministic_checks": [], "skill": "app"}],
    "requirements_map": [], "completion_criteria": ["works"],
    "human_decisions": [],
}


def _codex_jsonl(payload):
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message",
                             "text": json.dumps(payload)}}),
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": 1200,
                             "cached_input_tokens": 300,
                             "output_tokens": 450}}),
    ])


def _project_cfg(base_cfg, repo):
    cfg = copy.deepcopy(base_cfg)
    # the exact placeholder-laden legacy role config from config.yaml --
    # proves it never reaches Codex under the new resolver
    cfg["roles"] = {
        "conductor": {"provider": "anthropic",
                     "model": "example-reasoning-model"},
    }
    cfg["backends"] = {
        "codex": {"type": "cli", "kind": "codex", "binary": "codex",
                 "auth_probe_args": ["login", "status"]},
        "ollama": {"type": "local", "model": "qwen3.5:latest",
                  "cost_free": True},
    }
    cfg["routing"] = {"mode": "simple", "primary": None, "fallbacks": []}
    cfg["project"]["repository_root"] = str(repo)
    cfg["runtime"] = {"project_dir": str(repo / ".state")}
    return cfg


def test_registered_project_starts_with_codex_primary_ollama_fallback(
        sandbox):
    """#19/#20: the exact confirmed-failing command --
    `project start ... --primary codex --fallback ollama` -- now succeeds,
    and the mocked Codex command carries no --model flag."""
    from core.project import project_start
    repo, cfg = sandbox["repo"], _project_cfg(sandbox["cfg"], sandbox["repo"])
    plan = repo / "plan.md"
    plan.write_text("# Plan\n\nBuild a small app.\n", encoding="utf-8")

    runner = FakeRunner([{"stdout": _codex_jsonl(ARCHITECT_PAYLOAD)}])
    which = lambda b: "C:/bin/%s" % b   # noqa: E731
    result = project_start(cfg, str(plan),
                           overrides={"primary": "codex",
                                     "fallbacks": ["ollama"]},
                           runner=runner, which=which)
    assert result["status"] == "started", result
    codex_argv = runner.calls[0]["argv"]
    assert "--model" not in codex_argv
    assert "example-reasoning-model" not in codex_argv
    assert codex_argv[:4] == ["codex", "-a", "never", "exec"]


def test_project_start_fallback_from_codex_rebuilds_context_for_ollama(
        sandbox, monkeypatch):
    """#21: when Codex fails, the fallback to Ollama rebuilds the context
    package for Ollama's own budget (backend=<name> is threaded through
    compose()) and resolves to the configured qwen3.5:latest model."""
    from core.project import project_start
    import core.context.compose as compose_mod
    seen_backends = []
    real_compose = compose_mod.compose

    def spy_compose(cfg, role, role_prompt, input_data=None, schema=None,
                    **kw):
        seen_backends.append(kw.get("backend"))
        return real_compose(cfg, role, role_prompt, input_data, schema,
                            **kw)
    monkeypatch.setattr(compose_mod, "compose", spy_compose)

    repo, cfg = sandbox["repo"], _project_cfg(sandbox["cfg"], sandbox["repo"])
    plan = repo / "plan.md"
    plan.write_text("# Plan\n\nBuild a small app.\n", encoding="utf-8")

    runner = FakeRunner([
        {"exit_code": 1, "stdout": "", "stderr": "codex: internal error"},
        {"stdout": "ollama version 0.5.7"},                     # --version
        {"stdout": "NAME  ID  SIZE\nqwen3.5:latest  x  1GB\n"},  # list
    ])
    transport = OllamaStream([[ollama_event(
        content=json.dumps(ARCHITECT_PAYLOAD), done=True)]])
    which = lambda b: "C:/bin/%s" % b   # noqa: E731
    result = project_start(cfg, str(plan),
                           overrides={"primary": "codex",
                                     "fallbacks": ["ollama"]},
                           runner=runner, which=which, transport=transport)
    assert result["status"] == "started", result
    assert seen_backends[:2] == ["codex", "ollama"]
    assert transport.calls[0]["body"]["model"] == "qwen3.5:latest"


def test_project_start_reports_diagnostic_not_bare_model_unavailable(
        sandbox):
    """Both Codex and Ollama fail (Ollama via the confirmed typo bug) --
    project_start must return a safe, structured diagnostic, not just the
    bare string "model_unavailable"."""
    from core.project import project_start
    repo, cfg = sandbox["repo"], _project_cfg(sandbox["cfg"], sandbox["repo"])
    cfg["backends"]["ollama"]["model"] = "qwew3.5:latest"   # the typo
    plan = repo / "plan.md"
    plan.write_text("# Plan\n\nBuild a small app.\n", encoding="utf-8")

    runner = FakeRunner([
        {"exit_code": 1, "stdout": "", "stderr": "codex: internal error"},
        {"stdout": "ollama version 0.5.7"},
        {"stdout": "NAME  ID  SIZE\nqwen3.5:latest  x  1GB\n"},
    ])
    which = lambda b: "C:/bin/%s" % b   # noqa: E731
    result = project_start(cfg, str(plan),
                           overrides={"primary": "codex",
                                     "fallbacks": ["ollama"]},
                           runner=runner, which=which,
                           transport=Transport([]))
    assert result["status"] == "architect_failed"
    assert result["error"] == "model_unavailable"
    assert result["diagnostic"] is not None
    joined = "\n".join(result["diagnostic"])
    assert "role=architect" in joined
    assert "backend=ollama" in joined
    assert "qwew3.5:latest" not in joined or "configured_model" in joined
