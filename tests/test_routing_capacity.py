"""Ordered fallback routing, breaker behaviour (rate limit / usage
exhaustion / recovery estimation), and the deterministic capacity manager."""
import json

import pytest

from conftest import Clock, FakeRunner, Transport, oai_body, project_cfg
from core import errors
from core.backends import invoke_backend, routing_chain
from core.breaker import BreakerBoard
from core.capacity import (CapacityLedger, decide_start,
                           estimate_cycle_tokens)


def make_env(base_cfg, tmp_path):
    clock = Clock()
    ledger = CapacityLedger(base_cfg, str(tmp_path / "mem"), clock=clock)
    board = BreakerBoard(str(tmp_path / "mem"), clock=clock)
    return clock, ledger, board


def cli_backend_cfg(cfg):
    cfg["backends"] = {
        "codex": {"type": "cli", "kind": "codex", "binary": "codex"},
        "claude": {"type": "cli", "kind": "configured", "binary": "claude",
                   "invoke_args": ["-p"], "prompt_via": "stdin",
                   "parse": "text"},
        "mock_api": {"type": "api", "provider": "mock", "model": "m"},
    }
    cfg["routing"] = {"mode": "simple", "primary": "codex",
                      "fallbacks": ["claude", "mock_api"]}
    return cfg


CODEX_OK = {"stdout": json.dumps({"type": "item.completed",
                                  "item": {"type": "agent_message",
                                           "text": "codex did it"}})}


# 12. ordered fallback routing -----------------------------------------------------
def test_ordered_fallback_routing(base_cfg, tmp_path):
    cli_backend_cfg(base_cfg)
    clock, ledger, board = make_env(base_cfg, tmp_path)
    events = []
    # codex hits its usage limit; claude answers
    runner = FakeRunner([
        {"exit_code": 1, "stderr": "You've hit your usage limit. "
                                   "Try again in 2 hours."},
        {"exit_code": 0, "stdout": "claude did it"},
    ])
    result = invoke_backend(base_cfg, "codex", "coder", "prompt",
                            fallback_chain=["claude", "mock_api"],
                            ledger=ledger, board=board, runner=runner,
                            which=lambda b: "x", log=events.append)
    assert result["ok"] and result["backend"] == "claude"
    assert result["content"] == "claude did it"
    fallback = [e for e in events if e["event"] == "fallback"]
    assert fallback and fallback[0]["to"] == "claude" and \
        fallback[0]["reason"] == "usage_limit"
    # 16. usage-exhaustion breaker opened with the explicit retry hint
    assert board.state("codex") == "usage_exhausted"
    until = board.unavailable_until("codex")
    assert until == "2026-07-15T14:00:00"   # clock noon + 2h


def test_per_agent_routing_and_overrides(base_cfg):
    cli_backend_cfg(base_cfg)
    base_cfg["routing"]["mode"] = "per_agent"
    base_cfg["routing"]["per_agent"] = {
        "qa": {"primary": "mock_api", "fallbacks": ["claude"]}}
    assert routing_chain(base_cfg, "qa") == ["mock_api", "claude"]
    assert routing_chain(base_cfg, "coder") == ["codex", "claude", "mock_api"]
    assert routing_chain(base_cfg, "coder",
                         {"primary": "claude", "fallbacks": ["codex"]}) == \
        ["claude", "codex"]


# 13. no fallback on authentication failure -------------------------------------------
def test_no_fallback_on_auth_failure(base_cfg, tmp_path):
    cli_backend_cfg(base_cfg)
    clock, ledger, board = make_env(base_cfg, tmp_path)
    runner = FakeRunner([{"exit_code": 1, "stderr": "Not logged in"}])
    result = invoke_backend(base_cfg, "codex", "coder", "p",
                            fallback_chain=["claude"], ledger=ledger,
                            board=board, runner=runner, which=lambda b: "x")
    assert result["ok"] is False
    assert result["error"]["kind"] == "auth"
    assert len(runner.calls) == 1               # claude never attempted
    assert board.state("codex") == "authentication_required"


# 14. no fallback to bypass refusal ------------------------------------------------------
def test_no_fallback_on_refusal(base_cfg, tmp_path):
    base_cfg["backends"] = {
        "mock_api": {"type": "api", "provider": "mock", "model": "m"},
        "mock_api2": {"type": "api", "provider": "mock", "model": "m2"}}
    clock, ledger, board = make_env(base_cfg, tmp_path)
    transport = Transport([(200, oai_body("I'm sorry, I can't help with that."))])
    result = invoke_backend(base_cfg, "mock_api", "coder", "p",
                            fallback_chain=["mock_api2"], ledger=ledger,
                            board=board, transport=transport)
    assert result["ok"] is False and result["refusal"] is True
    assert len(transport.calls) == 1            # second backend never tried


# 15. rate-limit circuit breaker ------------------------------------------------------------
def test_rate_limit_breaker_blocks_then_reopens(base_cfg, tmp_path):
    cli_backend_cfg(base_cfg)
    clock, ledger, board = make_env(base_cfg, tmp_path)
    board.record_failure("codex", "rate_limit", retry_after_seconds=600)
    assert board.state("codex") == "rate_limited"
    # while open, invoke skips codex without calling it
    runner = FakeRunner([{"exit_code": 0, "stdout": "claude did it"}])
    events = []
    result = invoke_backend(base_cfg, "codex", "coder", "p",
                            fallback_chain=["claude"], ledger=ledger,
                            board=board, runner=runner, which=lambda b: "x",
                            log=events.append)
    assert result["backend"] == "claude"
    assert all("codex" != c["argv"][0] for c in runner.calls)
    assert any(e["event"] == "routing_skip" and e["backend"] == "codex"
               for e in events)
    # after the wait, breaker goes half-open (cooling) then available
    clock.advance(minutes=11)
    assert board.state("codex") == "cooling"
    board.mark_health_ok("codex")
    assert board.state("codex") == "available"


# 18. historical recovery estimation ------------------------------------------------------
def test_recovery_estimates_from_history_and_growth(base_cfg, tmp_path):
    clock, ledger, board = make_env(base_cfg, tmp_path)
    entry = board.entry("codex")
    entry["recovery_history_seconds"] = [1200, 1800]   # avg 1500s
    wait1 = board._wait_seconds(dict(entry, consecutive_failures=1),
                                "usage_limit", None, None)
    assert wait1 == 1500
    # still failing -> estimate grows
    wait3 = board._wait_seconds(dict(entry, consecutive_failures=3),
                                "usage_limit", None, None)
    assert wait3 == int(1500 * 1.5 ** 2)
    # observed downtime feeds history on recovery
    board.record_failure("claude", "rate_limit", retry_after_seconds=60)
    clock.advance(minutes=20)
    board.record_success("claude")
    assert board.entry("claude")["recovery_history_seconds"] == [1200]
    assert board.state("claude") == "available"


# 21-24. capacity estimation and start decisions -----------------------------------------------
def task(size="medium", security=False, skill="app-code"):
    return {"expected_size": size, "security_relevant": security,
            "skill": skill}


def test_estimate_labelled_and_safety_multiplier(base_cfg, tmp_path):
    project_cfg({"cfg": base_cfg})
    clock, ledger, board = make_env(base_cfg, tmp_path)
    est = estimate_cycle_tokens(base_cfg, task(), ledger, "mock")
    assert est["safety_multiplier"] == 1.35
    assert est["required_capacity_tokens"] == int(
        est["estimated_cycle_tokens"] * 1.35)
    # security review adds tokens; larger tasks cost more
    assert estimate_cycle_tokens(base_cfg, task(security=True), ledger,
                                 "mock")["estimated_cycle_tokens"] > \
        est["estimated_cycle_tokens"]
    assert estimate_cycle_tokens(base_cfg, task("large"), ledger,
                                 "mock")["estimated_cycle_tokens"] > \
        est["estimated_cycle_tokens"]
    # multiplier is configurable within safe limits (clamped)
    base_cfg["capacity"]["safety_multiplier"] = 99
    assert estimate_cycle_tokens(base_cfg, task(), ledger,
                                 "mock")["safety_multiplier"] == 3.0
    base_cfg["capacity"]["safety_multiplier"] = 1.35

    # 21. unknown capacity is labelled, never presented as a quota
    decision = decide_start(base_cfg, task(), ledger, board, ["mock"])
    assert decision["decision"] == "start"
    assert decision["confidence"] == "unknown"
    assert decision["available_estimated_tokens"] is None


def test_capacity_rejection_and_reroute(base_cfg, tmp_path):
    project_cfg({"cfg": base_cfg})
    clock, ledger, board = make_env(base_cfg, tmp_path)
    # 22. local limit nearly consumed -> primary rejected
    base_cfg["limits"] = {"mock": {"maximum_estimated_tokens_per_day": 1000}}
    ledger.record_call("mock", "coder", ok=True,
                       usage={"input_tokens": 600, "output_tokens": 300})
    # 23. a fallback with room takes over -> reroute
    base_cfg["limits"]["mock2"] = {
        "maximum_estimated_tokens_per_day": 10_000_000}
    decision = decide_start(base_cfg, task(), ledger, board,
                            ["mock", "mock2"])
    assert decision["decision"] == "reroute"
    assert decision["selected_backend"] == "mock2"
    assert decision["confidence"] == "estimated"
    assert "not provider quota" in decision["reason"]

    # reported capacity sufficient -> start (rule 1)
    decision = decide_start(base_cfg, task(), ledger, board, ["mock"],
                            reported_remaining={"mock": 10_000_000})
    assert decision["decision"] == "start"
    assert decision["confidence"] == "reported"

    # every backend breaker-open -> wait with the earliest reset
    board.record_failure("mock", "usage_limit", retry_after_seconds=3600)
    board.record_failure("mock2", "usage_limit", retry_after_seconds=7200)
    decision = decide_start(base_cfg, task(), ledger, board,
                            ["mock", "mock2"])
    assert decision["decision"] == "wait"
    assert decision["wait_until"] == "2026-07-15T13:00:00"

    # nothing configured/available at all -> human_required
    empty_board = BreakerBoard(str(tmp_path / "mem2"), clock=clock)
    base_cfg["limits"] = {"mock": {"maximum_calls_per_day": 0}}
    decision = decide_start(base_cfg, task(), ledger, empty_board, ["mock"])
    assert decision["decision"] == "human_required"


def test_self_imposed_limits_status(base_cfg, tmp_path):
    clock, ledger, board = make_env(base_cfg, tmp_path)
    base_cfg["limits"] = {"codex": {"maximum_calls_per_hour": 2}}
    ledger.record_call("codex", "coder", ok=True)
    assert ledger.limit_status("codex") == []
    ledger.record_call("codex", "coder", ok=True)
    assert any("maximum_calls_per_hour" in r
               for r in ledger.limit_status("codex"))
    # null limits: nothing invented, nothing blocked
    assert ledger.limit_status("claude") == []
