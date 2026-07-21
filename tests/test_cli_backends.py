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
    # global approval option MUST come before the `exec` subcommand:
    # `codex -a never exec ...`, never `codex exec --ask-for-approval never`
    assert argv[:4] == ["codex", "-a", "never", "exec"]
    assert "--ask-for-approval" not in argv
    assert "--ignore-user-config" in argv
    assert "--ephemeral" in argv and "--json" in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--cd") + 1] == "C:/ws"
    assert argv[-1] == "-"          # prompt via stdin
    # read-only sandbox for every non-editing role
    for role in ("architect", "conductor", "qa", "security"):
        argv = backend.build_argv(role, "read", "C:/ws")
        assert argv[argv.index("--sandbox") + 1] == "read-only"
    # write permission alone is not enough — role must be the coder
    argv = backend.build_argv("qa", "write", "C:/ws")
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_codex_command_construction_rejects_subcommand_placement_by_default():
    """Without an explicit capability-proven override, the subcommand-style
    `codex exec --ask-for-approval never` must never be generated."""
    backend = CodexCLIBackend("codex", {}, runner=FakeRunner([]),
                              which=lambda b: "x")
    argv = backend.build_argv("coder", "write", "C:/ws")
    assert not (argv[0] == "codex" and argv[1] == "exec" and
               "--ask-for-approval" in argv[1:3])
    assert argv.index("-a") < argv.index("exec")


def test_codex_command_construction_honours_subcommand_override():
    """The escape hatch for CLI versions where capability detection proves
    the subcommand flag is what's supported."""
    backend = CodexCLIBackend("codex", {"approval_placement": "subcommand"},
                              runner=FakeRunner([]), which=lambda b: "x")
    argv = backend.build_argv("coder", "write", "C:/ws")
    assert argv[:2] == ["codex", "exec"]
    assert argv[argv.index("--ask-for-approval") + 1] == "never"
    assert "-a" not in argv


def test_codex_command_construction_windows_path_with_spaces():
    backend = CodexCLIBackend("codex", {}, runner=FakeRunner([]),
                              which=lambda b: "x")
    workspace = r"C:/Users/Administrator/OneDrive - Office 365/Desktop/AgenticOS"
    argv = backend.build_argv("coder", "write", workspace)
    # argument arrays: the space-containing path stays a single element,
    # never split or shell-quoted
    assert argv[argv.index("--cd") + 1] == workspace
    assert workspace in argv
    assert not any(" " in a and a != workspace for a in argv)


def test_codex_command_construction_never_leaks_unrelated_backend_config():
    """Codex's own cfg dict is the only source of --model / extra_args --
    an unrelated Ollama model catalogue key must never surface in argv."""
    backend = CodexCLIBackend("codex", {"ollama_model": "llama3:8b",
                                        "model": "gpt-5-codex"},
                              runner=FakeRunner([]), which=lambda b: "x")
    argv = backend.build_argv("coder", "write", "C:/ws")
    assert "llama3:8b" not in argv
    assert argv[argv.index("--model") + 1] == "gpt-5-codex"


# -- central model resolution: Codex CLI ---------------------------------------------
@pytest.mark.parametrize("configured_model", [
    None,                       # 1. missing
    "auto",                     # 2. auto
    "example-reasoning-model",  # 3. placeholder role-style name (conductor)
    "example-coding-model",     # 4. placeholder role-style name (worker)
    "example/small-triage-model",
    "",
])
def test_codex_omits_model_flag_for_placeholder_or_missing(configured_model):
    cfg = {"model": configured_model} if configured_model is not None else {}
    backend = CodexCLIBackend("codex", cfg, runner=FakeRunner([]),
                              which=lambda b: "x")
    argv = backend.build_argv("architect", "read", "C:/ws")
    assert "--model" not in argv
    assert backend.last_model_resolution["resolved_model"] == "provider_default"
    assert backend.last_model_resolution["model_source"] == \
        "cli_provider_default"
    assert backend.last_model_resolution["model_flag_emitted"] is False
    assert backend.last_model_resolution["valid"] is True


def test_codex_includes_model_flag_for_explicit_valid_model():
    # 5. explicit valid configured model -> included
    backend = CodexCLIBackend("codex", {"model": "gpt-5-codex"},
                              runner=FakeRunner([]), which=lambda b: "x")
    argv = backend.build_argv("architect", "read", "C:/ws")
    assert argv[argv.index("--model") + 1] == "gpt-5-codex"
    assert backend.last_model_resolution["model_source"] == "explicit_config"
    assert backend.last_model_resolution["model_flag_emitted"] is True


def test_codex_architect_role_resolves_provider_default_and_succeeds():
    """#13: the architect role -- the exact role project_start() calls --
    with a placeholder backend model still succeeds via provider default,
    matching the confirmed-good manual smoke invocation shape."""
    runner = FakeRunner([{"stdout": CODEX_JSONL}])
    backend = CodexCLIBackend("codex", {"model": "example-reasoning-model"},
                              runner=runner, which=lambda b: "x")
    result = backend.invoke("architect", "plan this", None, "C:/ws", "read",
                            300)
    assert result["ok"]
    argv = runner.calls[0]["argv"]
    assert "--model" not in argv
    assert argv[:4] == ["codex", "-a", "never", "exec"]


# -- central model resolution: Claude CLI (via ConfiguredCLIBackend) -----------------
@pytest.mark.parametrize("configured_model", [
    None, "auto", "example-reasoning-model",
])
def test_claude_cli_omits_model_flag_for_placeholder_or_auto(configured_model):
    # 6/7
    cfg = dict(CLAUDE_CFG)
    if configured_model is not None:
        cfg["model"] = configured_model
    runner = FakeRunner([{"stdout": json.dumps(
        {"result": "did the thing", "model": "claude-default"})}])
    backend = ConfiguredCLIBackend("claude", cfg, runner=runner,
                                   which=lambda b: "x")
    result = backend.invoke("coder", "prompt", None, "C:/ws", "write", 60)
    assert result["ok"]
    argv = runner.calls[0]["argv"]
    assert "--model" not in argv
    assert backend.last_model_resolution["model_flag_emitted"] is False


def test_claude_cli_includes_model_flag_for_explicit_valid_model():
    cfg = dict(CLAUDE_CFG, model="claude-opus-4-8")
    runner = FakeRunner([{"stdout": json.dumps({"result": "ok"})}])
    backend = ConfiguredCLIBackend("claude", cfg, runner=runner,
                                   which=lambda b: "x")
    backend.invoke("coder", "prompt", None, "C:/ws", "write", 60)
    argv = runner.calls[0]["argv"]
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"


def test_codex_worker_and_verifier_roles_resolve_provider_default():
    """#14/#15: worker and verifier roles behave identically to architect
    under the same central resolver -- no role-specific special-casing."""
    for role in ("worker", "verifier"):
        runner = FakeRunner([{"stdout": CODEX_JSONL}])
        backend = CodexCLIBackend(
            "codex", {"model": "example-verification-model"}, runner=runner,
            which=lambda b: "x")
        result = backend.invoke(role, "do it", None, "C:/ws", "read", 300)
        assert result["ok"], role
        assert "--model" not in runner.calls[0]["argv"]


def test_codex_final_auditor_role_resolves_provider_default():
    """#16: the final-audit call reuses the "qa" role end to end."""
    runner = FakeRunner([{"stdout": CODEX_JSONL}])
    backend = CodexCLIBackend("codex", {"model": "example-verification-model"},
                              runner=runner, which=lambda b: "x")
    result = backend.invoke("qa", "final audit", None, "C:/ws", "read", 300)
    assert result["ok"]
    assert "--model" not in runner.calls[0]["argv"]


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


def test_codex_invoke_raises_on_terminal_error_event_even_with_exit_zero():
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "s1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "error", "message": "model overloaded"}),
    ])
    runner = FakeRunner([{"exit_code": 0, "stdout": stdout}])
    backend = CodexCLIBackend("codex", {}, runner=runner, which=lambda b: "x")
    with pytest.raises(errors.UnknownFailureError):
        backend.invoke("coder", "do it", None, "C:/ws", "write", 60)


# -- Codex smoke test: exact confirmed-working manual invocation ------------------------
CONFIRMED_SMOKE_JSONL = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "t1"}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message",
                         "text": "CODEX_SMOKE_OK"}}),
    json.dumps({"type": "turn.completed",
                "usage": {"input_tokens": 11776, "cached_input_tokens": 8960,
                          "output_tokens": 9, "reasoning_output_tokens": 0}}),
])


def _codex_backend(runner_responses, cfg=None):
    from providers.cli_codex import CodexCLIBackend as _Codex
    return _Codex("codex", cfg or {}, runner=FakeRunner(runner_responses),
                  which=lambda b: "x")


def test_codex_smoke_argv_matches_confirmed_working_invocation():
    backend = _codex_backend([
        {"stdout": ""}, {"stdout": ""},          # --help probes: no output
        {"stdout": CONFIRMED_SMOKE_JSONL}])
    ok = backend.smoke_test("C:/ws")
    assert ok is True
    smoke_argv = backend.runner.calls[-1]["argv"]
    assert smoke_argv[:4] == ["codex", "-a", "never", "exec"]
    assert "--ignore-user-config" in smoke_argv
    assert "--ephemeral" in smoke_argv
    assert "--json" in smoke_argv
    assert smoke_argv[smoke_argv.index("--sandbox") + 1] == "read-only"
    assert "workspace-write" not in smoke_argv
    assert "danger-full-access" not in smoke_argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in smoke_argv
    # prompt travels as a positional argument, not via stdin
    assert smoke_argv[-1].startswith("Reply with exactly:")
    assert backend.runner.calls[-1]["stdin"] is None


def test_codex_smoke_passes_on_confirmed_jsonl_output():
    from providers.cli_codex import evaluate_smoke_jsonl
    verdict = evaluate_smoke_jsonl(CONFIRMED_SMOKE_JSONL, exit_code=0,
                                   timed_out=False)
    assert verdict["ok"] is True
    assert verdict["event_types"] == ["thread.started", "turn.started",
                                      "item.completed", "turn.completed"]


def test_codex_smoke_passes_with_extra_jsonl_events_in_any_order():
    from providers.cli_codex import evaluate_smoke_jsonl
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.started", "item": {"id": "item_0"}}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message",
                             "text": "prelude\nCODEX_SMOKE_OK\nnote"}}),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ])
    verdict = evaluate_smoke_jsonl(stdout, exit_code=0, timed_out=False)
    assert verdict["ok"] is True


def test_codex_smoke_nonempty_stderr_is_not_a_failure():
    backend = _codex_backend([
        {"stdout": ""}, {"stdout": ""},
        {"stdout": CONFIRMED_SMOKE_JSONL,
         "stderr": "note: fetching latest model catalogue\n"}])
    assert backend.smoke_test("C:/ws") is True


def test_codex_smoke_fails_on_timeout():
    from providers.cli_codex import evaluate_smoke_jsonl
    verdict = evaluate_smoke_jsonl("", exit_code=None, timed_out=True)
    assert verdict["ok"] is False and verdict["reason"] == "timeout"


def test_codex_smoke_fails_on_nonzero_exit_code():
    from providers.cli_codex import evaluate_smoke_jsonl
    verdict = evaluate_smoke_jsonl(CONFIRMED_SMOKE_JSONL, exit_code=1,
                                   timed_out=False)
    assert verdict["ok"] is False and "exit code" in verdict["reason"]


def test_codex_smoke_fails_on_turn_failed_event():
    from providers.cli_codex import evaluate_smoke_jsonl
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "turn.failed", "error": "sandbox denied write"}),
    ])
    verdict = evaluate_smoke_jsonl(stdout, exit_code=0, timed_out=False)
    assert verdict["ok"] is False
    assert "sandbox denied write" in verdict["reason"]


def test_codex_smoke_fails_on_error_event():
    from providers.cli_codex import evaluate_smoke_jsonl
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "error", "message": "not logged in"}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message",
                             "text": "CODEX_SMOKE_OK"}}),
        json.dumps({"type": "turn.completed"}),
    ])
    verdict = evaluate_smoke_jsonl(stdout, exit_code=0, timed_out=False)
    assert verdict["ok"] is False
    assert "not logged in" in verdict["reason"]


def test_codex_smoke_fails_on_missing_agent_message():
    from providers.cli_codex import evaluate_smoke_jsonl
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "turn.completed"}),
    ])
    verdict = evaluate_smoke_jsonl(stdout, exit_code=0, timed_out=False)
    assert verdict["ok"] is False
    assert "agent_message" in verdict["reason"]


def test_codex_smoke_fails_on_missing_turn_completed():
    from providers.cli_codex import evaluate_smoke_jsonl
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message",
                             "text": "CODEX_SMOKE_OK"}}),
    ])
    verdict = evaluate_smoke_jsonl(stdout, exit_code=0, timed_out=False)
    assert verdict["ok"] is False
    assert "turn.completed" in verdict["reason"]


def test_codex_smoke_does_not_require_single_json_object():
    """Multiple independent JSONL lines are expected -- the parser must not
    assume the entire stdout is exactly one JSON document."""
    from providers.cli_codex import evaluate_smoke_jsonl
    assert len(CONFIRMED_SMOKE_JSONL.strip().splitlines()) > 1
    verdict = evaluate_smoke_jsonl(CONFIRMED_SMOKE_JSONL, exit_code=0,
                                   timed_out=False)
    assert verdict["ok"] is True


def test_codex_smoke_capability_detection_omits_unsupported_ignore_user_config():
    """`--ignore-user-config` must be omitted, not blindly assumed, when
    `codex --help` / `codex exec --help` show it isn't supported."""
    backend = _codex_backend([
        {"stdout": "codex [OPTIONS] <COMMAND>\n  -a, --ask-for-approval <MODE>\n"},
        {"stdout": "codex exec [OPTIONS] [PROMPT]\n  --ephemeral\n  --json\n"
                   "  --sandbox <MODE> [possible values: read-only, "
                   "workspace-write, danger-full-access]\n"},
        {"stdout": CONFIRMED_SMOKE_JSONL}])
    ok = backend.smoke_test("C:/ws")
    assert ok is True
    smoke_argv = backend.runner.calls[-1]["argv"]
    assert "--ignore-user-config" not in smoke_argv
    assert smoke_argv[:4] == ["codex", "-a", "never", "exec"]


def test_codex_smoke_capability_detection_uses_subcommand_when_global_unsupported():
    """When `codex --help` shows no global approval flag but `codex exec
    --help` does, fall back to the subcommand placement."""
    backend = _codex_backend([
        {"stdout": "codex [OPTIONS] <COMMAND>\n"},                    # no -a
        {"stdout": "codex exec [OPTIONS] [PROMPT]\n"
                   "  --ask-for-approval <MODE>\n"
                   "  --ignore-user-config\n  --ephemeral\n  --json\n"},
        {"stdout": CONFIRMED_SMOKE_JSONL}])
    ok = backend.smoke_test("C:/ws")
    assert ok is True
    smoke_argv = backend.runner.calls[-1]["argv"]
    assert smoke_argv[:2] == ["codex", "exec"]
    assert smoke_argv[smoke_argv.index("--ask-for-approval") + 1] == "never"
    assert "-a" not in smoke_argv


def test_codex_smoke_records_diagnostics_for_doctor_and_setup():
    backend = _codex_backend([
        {"stdout": ""}, {"stdout": ""},
        {"stdout": "", "exit_code": 1, "stderr": "not logged in"}])
    ok = backend.smoke_test("C:/ws")
    assert ok is False
    assert backend.last_smoke["reason"].startswith("nonzero exit code")
    assert backend.last_smoke["argv"][-1] == "<prompt redacted>"
    assert "Reply with exactly" not in json.dumps(backend.last_smoke)


def test_codex_smoke_never_calls_login_or_reads_credential_files():
    backend = _codex_backend([
        {"stdout": ""}, {"stdout": ""}, {"stdout": CONFIRMED_SMOKE_JSONL}])
    backend.smoke_test("C:/ws")
    for call in backend.runner.calls:
        joined = " ".join(call["argv"]).lower()
        assert "auth.json" not in joined
        assert "login status" not in joined  # auth probe is a separate call


def test_codex_auth_failure_classified_before_smoke_runs():
    with pytest.raises(errors.AuthError):
        classify_cli_failure("codex", 1, "Not logged in. Run codex login.")


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


def _ollama_list_runner(models):
    """FakeRunner scripted for detect_ollama()'s two calls: --version, list."""
    header = "NAME  ID  SIZE\n"
    rows = "\n".join("%s  x  1GB" % m for m in models)
    return FakeRunner([{"stdout": "ollama version 0.5.7"},
                       {"stdout": header + rows}])


def test_ollama_backend_requires_selected_model():
    backend = OllamaLocalBackend(
        "ollama", {}, transport=Transport([]),
        runner=_ollama_list_runner([]), which=lambda b: "C:/bin/ollama")
    with pytest.raises(errors.ModelUnavailableError):
        backend.invoke("coder", "x", None, ".", "write", 30)


def test_ollama_backend_invokes_local_model():
    transport = Transport([(200, oai_body("local says hi"))])
    backend = OllamaLocalBackend(
        "ollama", {"model": "llama3:8b"}, transport=transport,
        runner=_ollama_list_runner(["llama3:8b", "qwen3.5:latest"]),
        which=lambda b: "C:/bin/ollama")
    result = backend.invoke("coder", "x", None, ".", "write", 30)
    assert result["ok"] and result["backend_type"] == "local"
    assert result["estimated_cost_usd"] == 0.0
    assert transport.calls[0]["url"].startswith("http://localhost:11434")


# -- central model resolution: Ollama ----------------------------------------------------
def test_ollama_selected_qwen_model_is_used():
    """#8: the configured, installed model is sent to the HTTP endpoint
    unchanged -- this is the exact machine setup from the confirmed bug
    report (routing fallback: ollama, selected model qwen3.5:latest)."""
    transport = Transport([(200, oai_body("hello from qwen"))])
    backend = OllamaLocalBackend(
        "ollama", {"model": "qwen3.5:latest"}, transport=transport,
        runner=_ollama_list_runner(["qwen3.5:latest", "llama3:8b"]),
        which=lambda b: "C:/bin/ollama")
    result = backend.invoke("architect", "x", None, ".", "read", 30)
    assert result["ok"]
    assert backend.last_model_resolution["resolved_model"] == "qwen3.5:latest"
    assert backend.last_model_resolution["model_flag_emitted"] is True
    assert transport.calls[0]["body"].get("model") == "qwen3.5:latest"


def test_ollama_rejects_embedding_only_model():
    """#9: an embedding-only model is never selected, even if explicitly
    configured -- e.g. nomic-embed-text-v2-moe:latest."""
    backend = OllamaLocalBackend(
        "ollama", {"model": "nomic-embed-text-v2-moe:latest"},
        transport=Transport([]),
        runner=_ollama_list_runner(["nomic-embed-text-v2-moe:latest"]),
        which=lambda b: "C:/bin/ollama")
    with pytest.raises(errors.ModelUnavailableError) as exc:
        backend.invoke("coder", "x", None, ".", "write", 30)
    assert "embedding" in str(exc.value).lower()
    assert backend.last_model_resolution["valid"] is False


def test_ollama_rejects_missing_generation_model():
    """#10: only an embedding model is installed -- no generation model
    exists, so resolution must fail with a clear pre-invocation error."""
    backend = OllamaLocalBackend(
        "ollama", {}, transport=Transport([]),
        runner=_ollama_list_runner(["nomic-embed-text-v2-moe:latest"]),
        which=lambda b: "C:/bin/ollama")
    with pytest.raises(errors.ModelUnavailableError) as exc:
        backend.invoke("coder", "x", None, ".", "write", 30)
    assert "no installed generation model" in str(exc.value).lower() or \
        "no model configured" in str(exc.value).lower()


def test_ollama_typo_model_rejected_before_http_call():
    """The confirmed bug: a typo'd configured model (qwew3.5:latest vs
    qwen3.5:latest) must be caught by pre-invocation validation against
    `ollama list`, not surface as a live HTTP model_unavailable."""
    transport = Transport([(200, oai_body("should never be called"))])
    backend = OllamaLocalBackend(
        "ollama", {"model": "qwew3.5:latest"}, transport=transport,
        runner=_ollama_list_runner(["qwen3.5:latest"]),
        which=lambda b: "C:/bin/ollama")
    with pytest.raises(errors.ModelUnavailableError) as exc:
        backend.invoke("architect", "x", None, ".", "read", 30)
    assert "qwew3.5:latest" in str(exc.value)
    assert transport.calls == []   # never reached the HTTP layer


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


# -- central model resolution: API backends must never accept placeholders ------------
def test_api_backend_rejects_placeholder_model(base_cfg):
    # 11
    from core.backends import build_backend
    base_cfg["backends"] = {"mock_api": {"type": "api", "provider": "mock",
                                         "model": "example-reasoning-model"}}
    transport = Transport([(200, oai_body("should never be called"))])
    backend = build_backend(base_cfg, "mock_api", transport=transport)
    with pytest.raises(errors.ModelUnavailableError) as exc:
        backend.invoke("qa", "ping", None, ".", "read", 30)
    assert "explicit" in str(exc.value).lower()
    assert transport.calls == []   # never reached the HTTP layer


def test_api_backend_accepts_explicit_valid_model(base_cfg):
    # 12
    from core.backends import build_backend
    base_cfg["backends"] = {"mock_api": {"type": "api", "provider": "mock",
                                         "model": "mock-large-v1"}}
    transport = Transport([(200, oai_body("api alive"))])
    backend = build_backend(base_cfg, "mock_api", transport=transport)
    result = backend.invoke("qa", "ping", None, ".", "read", 30)
    assert result["ok"] and result["content"] == "api alive"
    assert backend.last_model_resolution["valid"] is True
    assert backend.last_model_resolution["model_source"] == "explicit_config"
