"""Fix for the project-start routing/fallback/timeout bug report: Codex was
silently skipped (a Windows stdin encoding bug in the shared subprocess
execution choke point, `core.execpolicy`) and the eventual Ollama fallback
hit one flat 900s timeout with no cold-start allowance and no record of
what happened to Codex. Covers: CLI-override precedence, CLI backends never
rejected for capacity/API-key/model-id reasons that only apply to API
backends, complete routing-attempt diagnostics, Ollama's own configurable
timeouts + cold-start detection, generation-model filtering, and the
UTF-8 stdin/stdout fix itself. No live provider/network call anywhere."""
import json

from conftest import (Clock, FakeRunner, OllamaStream, Transport, oai_body,
                      ollama_event)
from core import errors, execpolicy
from core.backends import invoke_backend, routing_chain
from core.breaker import BreakerBoard
from core.capacity import CapacityLedger
from providers.local_ollama import OllamaLocalBackend


def make_env(base_cfg, tmp_path):
    clock = Clock()
    ledger = CapacityLedger(base_cfg, str(tmp_path / "mem"), clock=clock)
    board = BreakerBoard(str(tmp_path / "mem"), clock=clock)
    return clock, ledger, board


def codex_and_ollama_cfg(cfg, *, ollama_model="qwen3.5:latest",
                        ollama_overrides=None):
    cfg["backends"] = {
        "codex": {"type": "cli", "kind": "codex", "binary": "codex"},
        "ollama": dict({"type": "local", "model": ollama_model},
                      **(ollama_overrides or {})),
    }
    cfg["routing"] = {"mode": "simple", "primary": "codex",
                      "fallbacks": ["ollama"]}
    return cfg


CODEX_OK = json.dumps({"type": "item.completed",
                       "item": {"type": "agent_message",
                                "text": "architected by codex"}})


# -- 1/5. CLI primary override precedence ------------------------------------------------

def test_cli_primary_override_reaches_architect(base_cfg):
    """#1/#3/#4 of the spec: an explicit --primary/--fallback override wins
    for every role that doesn't have its own higher-authority lock -- no
    per_agent/capability-mode config can silently defeat it."""
    codex_and_ollama_cfg(base_cfg)
    chain = routing_chain(base_cfg, "architect",
                          {"primary": "codex", "fallbacks": ["ollama"]})
    assert chain == ["codex", "ollama"]


def test_stale_role_level_config_does_not_defeat_explicit_primary(base_cfg):
    """The exact confirmed scenario: a machine/project role configuration
    (per_agent, or a stale routing.primary) must not silently override an
    explicit `project start --primary codex --fallback ollama`."""
    codex_and_ollama_cfg(base_cfg)
    base_cfg["routing"]["mode"] = "per_agent"
    base_cfg["routing"]["per_agent"] = {
        "architect": {"primary": "ollama", "fallbacks": []}}
    chain = routing_chain(base_cfg, "architect",
                          {"primary": "codex", "fallbacks": ["ollama"]})
    assert chain == ["codex", "ollama"]
    # capability-mode config is equally unable to defeat the override
    base_cfg["routing"]["mode"] = "capability"
    chain = routing_chain(base_cfg, "architect",
                          {"primary": "codex", "fallbacks": ["ollama"]})
    assert chain == ["codex", "ollama"]


# -- 2/3/4. Codex never rejected for API-shaped reasons ----------------------------------

def test_codex_selected_with_unknown_capacity(base_cfg, tmp_path):
    """No call history at all for codex -- capacity is genuinely unknown,
    never a rejection reason for a CLI subscription backend."""
    codex_and_ollama_cfg(base_cfg)
    clock, ledger, board = make_env(base_cfg, tmp_path)
    assert ledger.limit_status("codex") == []
    runner = FakeRunner([{"stdout": CODEX_OK}])
    result = invoke_backend(base_cfg, "codex", "architect", "p",
                            ledger=ledger, board=board, runner=runner,
                            which=lambda b: "x")
    assert result["ok"] and result["backend"] == "codex"


def test_codex_selected_without_api_keys_in_environment(base_cfg, tmp_path):
    """An empty environment (no *_API_KEY variables at all) must never
    block a CLI backend -- only API-type backends read api_key_env."""
    codex_and_ollama_cfg(base_cfg)
    clock, ledger, board = make_env(base_cfg, tmp_path)
    runner = FakeRunner([{"stdout": CODEX_OK}])
    result = invoke_backend(base_cfg, "codex", "architect", "p",
                            ledger=ledger, board=board, runner=runner,
                            which=lambda b: "x", env={})
    assert result["ok"]


def test_codex_selected_without_exact_model_id(base_cfg, tmp_path):
    """codex has no configured `model` (the CLI-auto case) -- resolution
    must let the authenticated CLI pick its own default, never reject it
    for lacking an exact model id the way an API backend would."""
    codex_and_ollama_cfg(base_cfg)   # codex cfg has no "model" key
    clock, ledger, board = make_env(base_cfg, tmp_path)
    runner = FakeRunner([{"stdout": CODEX_OK}])
    result = invoke_backend(base_cfg, "codex", "architect", "p",
                            ledger=ledger, board=board, runner=runner,
                            which=lambda b: "x")
    assert result["ok"]
    assert "--model" not in runner.calls[0]["argv"]


# -- 6/7/8. fallback rules ----------------------------------------------------------------

def test_recoverable_codex_failure_invokes_ollama_fallback(base_cfg, tmp_path):
    codex_and_ollama_cfg(base_cfg)
    clock, ledger, board = make_env(base_cfg, tmp_path)
    runner = FakeRunner([
        {"exit_code": 1, "stderr": "transient codex failure"},
        {"stdout": "ollama version 0.5.7"},
        {"stdout": "NAME  ID  SIZE\nqwen3.5:latest  x  1GB\n"},
    ])
    stream = OllamaStream([[ollama_event(content="architected by ollama",
                                        done=True)]])
    result = invoke_backend(base_cfg, "codex", "architect", "p",
                            fallback_chain=["ollama"], ledger=ledger,
                            board=board, runner=runner, transport=stream,
                            which=lambda b: "x")
    assert result["ok"] and result["backend"] == "ollama"
    assert result["content"] == "architected by ollama"


def test_auth_failure_never_invokes_fallback(base_cfg, tmp_path):
    codex_and_ollama_cfg(base_cfg)
    clock, ledger, board = make_env(base_cfg, tmp_path)
    runner = FakeRunner([{"exit_code": 1, "stderr": "Not logged in"}])
    result = invoke_backend(base_cfg, "codex", "architect", "p",
                            fallback_chain=["ollama"], ledger=ledger,
                            board=board, runner=runner, which=lambda b: "x")
    assert result["ok"] is False and result["error"]["kind"] == "auth"
    assert len(runner.calls) == 1   # ollama never attempted
    attempts = result["routing_attempts"]
    assert attempts[0]["backend"] == "codex" and attempts[0]["result"] == \
        "no_fallback"
    assert len(attempts) == 1   # ollama not even recorded as skipped


def test_refusal_never_invokes_fallback(base_cfg, tmp_path):
    base_cfg["backends"] = {
        "mock_api": {"type": "api", "provider": "mock", "model": "m"},
        "mock_api2": {"type": "api", "provider": "mock", "model": "m2"}}
    clock, ledger, board = make_env(base_cfg, tmp_path)
    transport = Transport([(200, oai_body("I'm sorry, I can't help with "
                                          "that."))])
    result = invoke_backend(base_cfg, "mock_api", "architect", "p",
                            fallback_chain=["mock_api2"], ledger=ledger,
                            board=board, transport=transport)
    assert result["ok"] is False and result["refusal"] is True
    assert len(transport.calls) == 1
    attempts = result["routing_attempts"]
    assert attempts[0]["result"] == "refused"
    assert len(attempts) == 1


# -- 9. complete routing-attempt diagnostics -----------------------------------------------

def test_complete_routing_attempts_when_primary_is_breaker_blocked(
        base_cfg, tmp_path):
    """The exact failure mode the bug report hit: a diagnostic that only
    shows the final fallback's failure, with zero record of what happened
    to the primary. Every backend in the chain must appear."""
    codex_and_ollama_cfg(base_cfg)
    clock, ledger, board = make_env(base_cfg, tmp_path)
    board.record_failure("codex", "unknown")
    board.record_failure("codex", "unknown")
    board.record_failure("codex", "unknown")   # 3rd -> breaker opens
    assert board.state("codex") == "unavailable"
    runner = FakeRunner([
        {"stdout": "ollama version 0.5.7"},
        {"stdout": "NAME  ID  SIZE\nqwen3.5:latest  x  1GB\n"},
    ])
    stream = OllamaStream([[errors.TimeoutError_(
        "ollama total timeout after 1800.0s")]])
    result = invoke_backend(base_cfg, "codex", "architect", "p",
                            fallback_chain=["ollama"], ledger=ledger,
                            board=board, runner=runner, transport=stream,
                            which=lambda b: "x")
    assert result["ok"] is False
    attempts = {a["backend"]: a for a in result["routing_attempts"]}
    assert set(attempts) == {"codex", "ollama"}
    assert attempts["codex"]["eligible"] is False
    assert attempts["codex"]["attempted"] is False
    assert attempts["codex"]["result"] == "skipped"
    assert "breaker" in attempts["codex"]["reason"].lower()
    assert attempts["ollama"]["eligible"] is True
    assert attempts["ollama"]["attempted"] is True
    assert attempts["ollama"]["result"] == "failure"


# -- 13. generation-model filtering (5-stage timeout model + native
# streaming, context sizing, keep_alive, thinking, unload: see
# tests/test_ollama_native.py) -----------------------------------------------

def test_ollama_never_selects_embedding_model_as_generation_worker(tmp_path):
    """#14 of the spec: nomic-embed-text-v2-moe (or any embedding model)
    is never selected as a worker or architect, even if it's the only
    installed model."""
    from core import errors as _errors
    backend = OllamaLocalBackend(
        "ollama", {}, transport=Transport([]),
        runner=FakeRunner([
            {"stdout": "ollama version 0.5.7"},
            {"stdout": "NAME  ID  SIZE\n"
                       "nomic-embed-text-v2-moe:latest  x  1GB\n"}]),
        which=lambda b: "C:/bin/ollama")
    try:
        backend.invoke("architect", "p", None, ".", "read", 30)
        assert False, "expected ModelUnavailableError"
    except _errors.ModelUnavailableError as exc:
        assert "embedding" in str(exc).lower() or \
            "no installed generation model" in str(exc).lower()


def test_ollama_does_not_apply_its_long_timeout_to_codex(base_cfg, tmp_path):
    """Ollama's own (possibly much longer) timeout must never leak onto
    Codex/Claude -- they keep using execution.command_timeout_seconds."""
    codex_and_ollama_cfg(base_cfg,
                        ollama_overrides={"total_timeout_seconds": 3600})
    clock, ledger, board = make_env(base_cfg, tmp_path)
    calls = []

    class RecordingRunner(FakeRunner):
        def __call__(self, argv, cwd=None, timeout=120, stdin_text=None):
            calls.append(timeout)
            return super().__call__(argv, cwd=cwd, timeout=timeout,
                                    stdin_text=stdin_text)

    runner = RecordingRunner([{"stdout": CODEX_OK}])
    invoke_backend(base_cfg, "codex", "architect", "p", ledger=ledger,
                  board=board, runner=runner, which=lambda b: "x")
    # codex's own invocation never sees Ollama's 3600s timeout: the
    # command_timeout_seconds default (900) still governs it
    assert 3600 not in calls


# -- Windows project paths ---------------------------------------------------------------

def test_codex_argv_handles_windows_project_paths(tmp_path):
    from providers.cli_codex import CodexCLIBackend
    backend = CodexCLIBackend("codex", {}, runner=FakeRunner([]),
                              which=lambda b: "x")
    workspace = r"C:\Users\Administrator\OneDrive - Office 365\Desktop\AgenticOS"
    argv = backend.build_argv("architect", "read", workspace)
    assert workspace in argv
    assert argv[argv.index(workspace) - 1] == "--cd"


# -- execpolicy UTF-8 fix -----------------------------------------------------------------

def test_execpolicy_stdin_stdout_roundtrip_non_ascii():
    """The confirmed root cause: subprocess.run(text=True) without an
    explicit encoding falls back to the Windows locale codepage, which
    silently mis-encodes a prompt containing any character outside it --
    exactly the "input is not valid UTF-8 (invalid byte at offset 275)"
    Codex reported. Must round-trip correctly now."""
    tricky = "hello — 你好 ‘quote’"
    result = execpolicy.run_command(
        ["python", "-c",
         "import sys; data = sys.stdin.buffer.read(); "
         "sys.stdout.buffer.write(data)"],
        cwd=".", timeout=10, stdin_text=tricky)
    assert result["exit_code"] == 0
    assert result["stdout"] == tricky


# -- circuit-breaker reset + fresh-verification recovery ---------------------------------

def test_stale_breaker_recovers_on_fresh_successful_verification(tmp_path):
    from core.authx import record_verification
    memory_dir = str(tmp_path / "mem")
    board = BreakerBoard(memory_dir)
    board.record_failure("codex", "unknown")
    board.record_failure("codex", "unknown")
    board.record_failure("codex", "unknown")
    assert board.state("codex") == "unavailable"
    record_verification(memory_dir, "codex", True, "smoke passed")
    assert BreakerBoard(memory_dir).state("codex") == "available"


def test_breaker_reset_discloses_previous_state_never_conceals_retry_after(
        tmp_path):
    board = BreakerBoard(str(tmp_path / "mem"))
    board.record_failure("codex", "rate_limit", retry_after_seconds=1200)
    assert board.state("codex") == "rate_limited"
    previous = board.reset("codex")
    assert previous["state"] == "rate_limited"
    assert previous["unavailable_until"] is not None   # disclosed, not hidden
    assert board.state("codex") == "available"


def test_recover_if_verified_never_overrides_provider_stated_rate_limit(
        tmp_path):
    """A rate/usage limit is the PROVIDER's own statement of when it will
    accept calls again -- a local smoke pass must never conceal/override
    that active retry-after."""
    board = BreakerBoard(str(tmp_path / "mem"))
    board.record_failure("codex", "rate_limit", retry_after_seconds=1200)
    changed = board.recover_if_verified("codex")
    assert changed is False
    assert board.state("codex") == "rate_limited"
