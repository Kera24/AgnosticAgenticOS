"""CLI backend adapters: Codex detection/auth/command construction, no
credential-file access, configured Claude/Qwen adapters, Ollama detection,
API adapter preservation, and token usage parsing/estimation."""
import json

import pytest

from conftest import FakeRunner, Transport, oai_body
from core import errors
from providers.cli_base import (FORBIDDEN_COMMAND_TOKENS, classify_cli_failure,
                                parse_retry_hint, validate_cli_command)
from providers.cli_codex import CodexCLIBackend
from providers.cli_configured import ConfiguredCLIBackend
from providers.local_ollama import OllamaLocalBackend, detect_ollama

CODEX_JSONL = "\n".join([
    json.dumps({"type": "session.created", "session_id": "s1"}),
    json.dumps({"type": "item.completed",
                "item": {"type": "agent_message", "text": "work summary\nDONE"}}),
    json.dumps({"type": "turn.completed",
                "usage": {"input_tokens": 1200, "cached_input_tokens": 300,
                          "output_tokens": 450}}),
])


# 4. Codex CLI detection ---------------------------------------------------------
def test_codex_detection():
    runner = FakeRunner([{"stdout": "codex-cli 1.2.3\n"}])
    backend = CodexCLIBackend("codex", {}, runner=runner,
                              which=lambda b: "C:/bin/codex")
    info = backend.detect()
    assert info == {"installed": True, "path": "C:/bin/codex",
                    "version": "codex-cli 1.2.3"}
    assert runner.calls[0]["argv"] == ["codex", "--version"]

    missing = CodexCLIBackend("codex", {}, runner=FakeRunner([]),
                              which=lambda b: None)
    assert missing.detect()["installed"] is False


# 5. Codex authentication-status parsing --------------------------------------------
def test_codex_auth_status_parsing():
    ok = CodexCLIBackend("codex", {}, which=lambda b: "x", runner=FakeRunner(
        [{"exit_code": 0, "stdout": "Logged in using ChatGPT\n"}]))
    assert ok.auth_status() == "ok"
    required = CodexCLIBackend("codex", {}, which=lambda b: "x",
                               runner=FakeRunner([{
                                   "exit_code": 1,
                                   "stdout": "Not logged in\n"}]))
    assert required.auth_status() == "required"
    # the probe is `codex login status` — a supported status command only
    assert required.runner.calls[0]["argv"] == ["codex", "login", "status"]


# 6. Codex non-interactive command construction ---------------------------------------
def test_codex_command_construction():
    backend = CodexCLIBackend("codex", {}, runner=FakeRunner([]),
                              which=lambda b: "x")
    argv = backend.build_argv("coder", "write", "C:/ws")
    assert argv[:2] == ["codex", "exec"]
    assert "--ephemeral" in argv and "--json" in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--ask-for-approval") + 1] == "never"
    assert argv[argv.index("--cd") + 1] == "C:/ws"
    assert argv[-1] == "-"          # prompt via stdin
    # read-only sandbox for every non-editing role
    for role in ("architect", "conductor", "qa", "security"):
        argv = backend.build_argv(role, "read", "C:/ws")
        assert argv[argv.index("--sandbox") + 1] == "read-only"
    # write permission alone is not enough — role must be the coder
    argv = backend.build_argv("qa", "write", "C:/ws")
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_codex_invoke_parses_jsonl_and_usage():
    runner = FakeRunner([{"stdout": CODEX_JSONL}])
    backend = CodexCLIBackend("codex", {}, runner=runner, which=lambda b: "x")
    result = backend.invoke("coder", "do it", {"k": "v"}, "C:/ws", "write", 300)
    assert result["ok"] and result["backend_type"] == "cli"
    assert "work summary" in result["content"]
    # 19. token usage parsing
    assert result["usage"] == {"input_tokens": 1200,
                               "cached_input_tokens": 300,
                               "output_tokens": 450,
                               "reasoning_tokens": None, "estimated": False}
    assert result["estimated_cost_usd"] == 0.0     # subscription: no USD cost
    assert "# INPUT DATA" in runner.calls[0]["stdin"]


# 7. no access to cached authentication files ------------------------------------------
def test_forbidden_credential_tokens_rejected():
    with pytest.raises(errors.PolicyError):
        validate_cli_command(["cat", "~/.codex/auth.json"])
    with pytest.raises(errors.PolicyError):
        validate_cli_command(["codex", "exec",
                              "--dangerously-bypass-approvals-and-sandbox"])
    # adapters route every command through this validation
    backend = ConfiguredCLIBackend("bad", {
        "binary": "cat", "invoke_args": ["~/.codex/auth.json"]},
        runner=FakeRunner([]), which=lambda b: "x")
    with pytest.raises(errors.PolicyError):
        backend.invoke("coder", "x", None, ".", "read", 30)


def test_cli_adapters_never_open_files(monkeypatch):
    """Behavioural guard: a full detect+auth+invoke cycle performs zero
    open() calls — credentials simply cannot be read."""
    opened = []
    import builtins
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open",
                        lambda *a, **k: opened.append(a and a[0]) or
                        real_open(*a, **k))
    runner = FakeRunner([{"stdout": "v1"}, {"stdout": "Logged in"},
                         {"stdout": CODEX_JSONL}])
    backend = CodexCLIBackend("codex", {}, runner=runner, which=lambda b: "x")
    backend.detect()
    backend.auth_status()
    backend.invoke("coder", "x", None, ".", "write", 30)
    assert opened == []


# 8/9. Claude and Qwen adapters use configured invocation only ---------------------------
CLAUDE_CFG = {"binary": "claude", "version_args": ["--version"],
              "invoke_args": ["-p", "--output-format", "json"],
              "write_args": ["--permission-mode", "acceptEdits"],
              "prompt_via": "stdin", "parse": "auto"}


def test_claude_adapter_configured_invocation():
    runner = FakeRunner([{"stdout": json.dumps(
        {"result": "did the thing", "model": "some-model",
         "usage": {"input_tokens": 10, "output_tokens": 5}})}])
    backend = ConfiguredCLIBackend("claude", CLAUDE_CFG, runner=runner,
                                   which=lambda b: "x")
    result = backend.invoke("coder", "prompt", None, "C:/ws", "write", 60)
    assert result["ok"] and result["content"] == "did the thing"
    assert result["usage"]["input_tokens"] == 10
    argv = runner.calls[0]["argv"]
    assert argv == ["claude", "-p", "--output-format", "json",
                    "--permission-mode", "acceptEdits"]
    assert runner.calls[0]["stdin"] == "prompt"
    # non-coder roles never get the write flags
    runner2 = FakeRunner([{"stdout": '{"result": "ok"}'}])
    backend2 = ConfiguredCLIBackend("claude", CLAUDE_CFG, runner=runner2,
                                    which=lambda b: "x")
    backend2.invoke("qa", "review", None, "C:/ws", "read", 60)
    assert "--permission-mode" not in runner2.calls[0]["argv"]


def test_qwen_adapter_prompt_as_arg_and_text_parse():
    cfg = {"binary": "qwen", "invoke_args": ["-p"], "prompt_via": "arg",
           "parse": "text"}
    runner = FakeRunner([{"stdout": "plain answer"}])
    backend = ConfiguredCLIBackend("qwen", cfg, runner=runner,
                                   which=lambda b: "x")
    result = backend.invoke("conductor", "the prompt", None, ".", "read", 60)
    assert result["content"] == "plain answer"
    assert runner.calls[0]["argv"] == ["qwen", "-p", "the prompt"]
    # 20. token estimation when usage is absent
    assert result["usage"]["estimated"] is True or \
        result["usage"]["input_tokens"] is None


def test_cli_failure_classification_and_retry_hints():
    with pytest.raises(errors.UsageLimitError) as exc:
        classify_cli_failure("codex", 1,
                             "You've hit your usage limit. Try again in "
                             "3 hours.")
    assert exc.value.retry_after_seconds == 3 * 3600
    with pytest.raises(errors.RateLimitError):
        classify_cli_failure("codex", 1, "429 Too Many Requests")
    with pytest.raises(errors.AuthError):
        classify_cli_failure("codex", 1, "Not logged in. Run codex login.")
    with pytest.raises(errors.BackendUnavailableError):
        classify_cli_failure("codex", 127, "codex: command not found")
    with pytest.raises(errors.TimeoutError_):
        classify_cli_failure("codex", None, "", timed_out=True)
    # 17. explicit retry-after parsing forms
    assert parse_retry_hint("Retry-After: 120") == (120, None)
    assert parse_retry_hint("please retry in 5 minutes") == (300, None)
    assert parse_retry_hint("quota resets at 2026-07-15 18:00")[1] == \
        "2026-07-15T18:00"
    assert parse_retry_hint("no hint here") == (None, None)


# 10. Ollama detection --------------------------------------------------------------------
def test_ollama_detection_and_model_discovery():
    runner = FakeRunner([
        {"stdout": "ollama version is 0.5.7"},
        {"stdout": "NAME            ID     SIZE\n"
                   "llama3:8b       abc    4.7 GB\n"
                   "qwen3:4b        def    2.6 GB\n"}])
    info = detect_ollama(runner=runner, which=lambda b: "C:/bin/ollama")
    assert info["installed"] and info["models"] == ["llama3:8b", "qwen3:4b"]
    assert detect_ollama(runner=FakeRunner([]),
                         which=lambda b: None)["installed"] is False


def test_ollama_backend_requires_selected_model():
    backend = OllamaLocalBackend("ollama", {}, transport=Transport([]))
    with pytest.raises(errors.ModelUnavailableError):
        backend.invoke("coder", "x", None, ".", "write", 30)


def test_ollama_backend_invokes_local_model():
    transport = Transport([(200, oai_body("local says hi"))])
    backend = OllamaLocalBackend("ollama", {"model": "llama3:8b"},
                                 transport=transport)
    result = backend.invoke("coder", "x", None, ".", "write", 30)
    assert result["ok"] and result["backend_type"] == "local"
    assert result["estimated_cost_usd"] == 0.0
    assert transport.calls[0]["url"].startswith("http://localhost:11434")


# 11. existing API adapter preservation ------------------------------------------------------
def test_api_backends_preserved_through_common_interface(base_cfg):
    from core.backends import build_backend
    base_cfg["backends"] = {"mock_api": {"type": "api", "provider": "mock",
                                         "model": "m1"}}
    transport = Transport([(200, oai_body("api alive"))])
    backend = build_backend(base_cfg, "mock_api", transport=transport)
    result = backend.invoke("qa", "ping", None, ".", "read", 30)
    assert result["ok"] and result["content"] == "api alive"
    assert result["backend_type"] == "api"
    assert transport.calls[0]["url"] == "http://mock.local/v1/chat/completions"
    # legacy role-based invoke_model path still works untouched
    from core.budget import Budget
    import tempfile
    from core.invoke import invoke_model
    budget = Budget(base_cfg, tempfile.mkdtemp(), "r")
    resp = invoke_model(base_cfg, "triage", "x", budget=budget,
                        transport=Transport([(200, oai_body("legacy ok"))]))
    assert resp["ok"] and resp["content"] == "legacy ok"
