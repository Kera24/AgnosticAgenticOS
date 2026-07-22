"""Provider selection, role/model routing, env overrides, OpenAI-compatible
operation, refusal detection, timeout handling, fallback routing."""
import json

import pytest

import providers
from conftest import AGENTIC_SRC, Transport, oai_body
from core import errors
from core.budget import Budget
from core.config import load_config
from core.invoke import invoke_model


# 1. provider selection ------------------------------------------------------
def test_provider_selection_by_type():
    from providers.anthropic import AnthropicProvider
    from providers.custom_command import CustomCommandProvider
    from providers.ollama import OllamaProvider
    from providers.openai import OpenAIProvider
    from providers.openai_compatible import OpenAICompatibleProvider

    assert isinstance(providers.build("openai", {"type": "openai"}), OpenAIProvider)
    assert isinstance(providers.build("anthropic", {"type": "anthropic"}),
                      AnthropicProvider)
    assert isinstance(providers.build("qwen", {"type": "openai_compatible"}),
                      OpenAICompatibleProvider)
    assert isinstance(providers.build("ollama", {"type": "ollama"}), OllamaProvider)
    assert isinstance(providers.build("cli", {"type": "custom_command",
                                              "command": ["x"]}),
                      CustomCommandProvider)
    with pytest.raises(KeyError):
        providers.build("nope", {"type": "does-not-exist"})


def test_ollama_defaults_keyless_and_costfree():
    p = providers.build("ollama", {"type": "ollama"})
    assert p.cost_free
    assert p.base_url() == "http://localhost:11434"   # native /api/chat


# 2. role-based model selection ----------------------------------------------
def test_each_role_uses_its_configured_model(base_cfg, budget):
    for role, expected in (("triage", "triage-model"), ("worker", "worker-model")):
        transport = Transport([(200, oai_body("hello"))])
        resp = invoke_model(base_cfg, role, "prompt", budget=budget,
                            transport=transport)
        assert resp["ok"]
        assert transport.calls[0]["body"]["model"] == expected


# 3. environment-variable overrides ------------------------------------------
def test_env_overrides():
    env = {"AGENTIC_ROLE_TRIAGE_MODEL": "override-model",
           "AGENTIC_ROLE_TRIAGE_PROVIDER": "ollama",
           "AGENTIC_EXECUTION_MODE": "auto",
           "AGENTIC_BUDGET_DAILY_LIMIT_USD": "9.5",
           "AGENTIC_PROVIDER_QWEN_BASE_URL": "http://q.local/v1"}
    cfg = load_config(path=str(AGENTIC_SRC / "config.yaml"), env=env)
    assert cfg["roles"]["triage"]["model"] == "override-model"
    assert cfg["roles"]["triage"]["provider"] == "ollama"
    assert cfg["execution"]["mode"] == "auto"
    assert cfg["budget"]["daily_limit_usd"] == 9.5
    assert cfg["providers"]["qwen"]["base_url"] == "http://q.local/v1"


# 4. OpenAI-compatible provider operation --------------------------------------
def test_openai_compatible_end_to_end(base_cfg, budget):
    transport = Transport([(200, oai_body("the answer", in_tok=42, out_tok=7))])
    resp = invoke_model(base_cfg, "worker", "do the thing", budget=budget,
                        transport=transport)
    assert resp["ok"] is True
    assert resp["content"] == "the answer"
    assert resp["usage"] == {"input_tokens": 42, "output_tokens": 7,
                             "cached_tokens": 0}
    assert resp["finish_reason"] == "stop"
    assert resp["error"] is None
    call = transport.calls[0]
    assert call["url"] == "http://mock.local/v1/chat/completions"
    assert "Authorization" not in call["headers"]  # keyless provider


def test_http_error_taxonomy(base_cfg, budget):
    cases = [(401, "auth"), (429, "rate_limit"), (404, "model_unavailable"),
             (500, "provider_error")]
    for status, kind in cases:
        transport = Transport([(status, "boom")] * 3)
        resp = invoke_model(base_cfg, "worker", "x", budget=budget,
                            transport=transport)
        assert resp["ok"] is False
        assert resp["error"]["kind"] == kind


def test_context_length_error(base_cfg, budget):
    transport = Transport([(400, json.dumps(
        {"error": {"message": "maximum context length exceeded"}}))])
    resp = invoke_model(base_cfg, "worker", "x", budget=budget,
                        transport=transport)
    assert resp["error"]["kind"] == "context_length"


# 7. refusal detection ---------------------------------------------------------
def test_refusal_detected_from_text(base_cfg, budget):
    transport = Transport([(200, oai_body("I'm sorry, I can't help with that."))])
    resp = invoke_model(base_cfg, "worker", "x", budget=budget,
                        transport=transport)
    assert resp["refusal"] is True
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "refusal"


def test_refusal_detected_from_metadata(base_cfg, budget):
    transport = Transport([(200, oai_body("", refusal="cannot comply"))])
    resp = invoke_model(base_cfg, "worker", "x", budget=budget,
                        transport=transport)
    assert resp["refusal"] is True


# 8. timeout handling ----------------------------------------------------------
def test_timeout_retries_then_reports(base_cfg, budget):
    transport = Transport([errors.TimeoutError_("slow"),
                           errors.TimeoutError_("slow")])
    sleeps = []
    resp = invoke_model(base_cfg, "worker", "x", budget=budget,
                        transport=transport, sleeper=sleeps.append)
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "timeout"
    assert len(transport.calls) == 2      # retried once on same provider
    assert len(sleeps) == 1


# 9. fallback routing ----------------------------------------------------------
def _add_fallback(cfg):
    cfg["providers"]["backup"] = {"type": "openai_compatible",
                                  "base_url": "http://backup.local/v1",
                                  "api_key_required": False, "cost_free": True}
    cfg["roles"]["worker"]["fallback_role"] = "worker_fallback"
    cfg["roles"]["worker_fallback"] = {"provider": "backup",
                                       "model": "backup-model",
                                       "temperature": 0,
                                       "max_output_tokens": 500, "tools": []}


def test_fallback_on_rate_limit(base_cfg, budget):
    _add_fallback(base_cfg)
    events = []
    primary_then_backup = Transport([(429, "slow down"), (429, "slow down"),
                                     (200, oai_body("from backup"))])
    resp = invoke_model(base_cfg, "worker", "x", budget=budget,
                        transport=primary_then_backup, log=events.append,
                        sleeper=lambda s: None)
    assert resp["ok"] is True
    assert resp["content"] == "from backup"
    assert primary_then_backup.calls[-1]["url"].startswith("http://backup.local")
    fallback_events = [e for e in events if e.get("event") == "fallback"]
    assert fallback_events and fallback_events[0]["reason"] == "rate_limit"
    assert fallback_events[0]["from_model"] == "worker-model"


def test_no_fallback_on_auth_error(base_cfg, budget):
    _add_fallback(base_cfg)
    transport = Transport([(401, "bad key")])
    resp = invoke_model(base_cfg, "worker", "x", budget=budget,
                        transport=transport)
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "auth"
    assert len(transport.calls) == 1      # auth never retries or falls back
