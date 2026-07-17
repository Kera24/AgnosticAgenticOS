"""Phase 8 — prompt caching: stable prefix ordering, API-only cache
fields, honest CLI labelling, telemetry that separates estimates from
provider-reported values."""
import json

from conftest import FakeRunner, Transport, oai_body
from core.context.broker import (CACHE_BOUNDARY, ContextBroker,
                                 split_cache_boundary,
                                 strip_cache_boundary)
from core.context.items import ContextItem, ContextRequest


def build_package(caching_enabled=True):
    cfg = {"context": {}, "caching": {"enabled": caching_enabled}}
    broker = ContextBroker(cfg)
    items = [
        ContextItem("policy", "NEVER push.", relevance_score=1.0),
        ContextItem("role_contract", "You are QA.", relevance_score=1.0),
        ContextItem("project_summary", "A web app.", relevance_score=0.7),
        ContextItem("work_order", "Review task t1.", relevance_score=1.0),
        ContextItem("code", "def f(): pass", source_path="a.py",
                    trust_level="untrusted"),
    ]
    return broker.build(ContextRequest(role="qa"), items)


# -- broker boundary -------------------------------------------------------------

def test_stable_prefix_before_boundary_dynamic_after():
    package = build_package()
    prefix, dynamic = split_cache_boundary(package.rendered)
    assert dynamic is not None
    assert "# OS POLICY" in prefix and "# PROJECT SUMMARY" in prefix
    assert "# WORK ORDER" in dynamic and "# CODE CONTEXT" in dynamic
    assert "WORK ORDER" not in prefix
    assert package.stable_prefix_chars == len(prefix)


def test_boundary_absent_when_caching_disabled():
    package = build_package(caching_enabled=False)
    assert CACHE_BOUNDARY not in package.rendered
    assert strip_cache_boundary(package.rendered) == package.rendered


def test_deterministic_prefix_across_builds():
    """Byte-identical stable prefix across builds — the whole point of
    prefix caching."""
    p1, _ = split_cache_boundary(build_package().rendered)
    p2, _ = split_cache_boundary(build_package().rendered)
    assert p1 == p2


# -- anthropic adapter ---------------------------------------------------------------

def anthropic_body(cache_read=0, cache_creation=None):
    usage = {"input_tokens": 50, "output_tokens": 5,
             "cache_read_input_tokens": cache_read}
    if cache_creation is not None:
        usage["cache_creation_input_tokens"] = cache_creation
    return json.dumps({"content": [{"type": "text", "text": "ok"}],
                       "stop_reason": "end_turn", "model": "m",
                       "usage": usage})


def anthropic_provider(transport, **cfg_over):
    from providers.anthropic import AnthropicProvider
    cfg = {"api_key_env": "K", **cfg_over}
    return AnthropicProvider("anthropic", cfg, transport=transport,
                             env={"K": "test-key"})


def test_anthropic_cache_control_on_marked_prompt():
    transport = Transport([(200, anthropic_body(cache_creation=40))])
    provider = anthropic_provider(transport)
    prompt = "STABLE POLICY" + CACHE_BOUNDARY + "DYNAMIC TASK"
    response = provider.invoke("m", prompt)
    payload = transport.calls[0]["body"]
    assert payload["system"][0]["text"] == "STABLE POLICY"
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"][0]["content"] == "DYNAMIC TASK"
    assert CACHE_BOUNDARY.strip() not in json.dumps(payload)
    assert response["usage"]["cache_creation_tokens"] == 40


def test_anthropic_one_hour_ttl_configurable():
    transport = Transport([(200, anthropic_body())])
    provider = anthropic_provider(transport, cache_ttl="1h")
    provider.invoke("m", "S" + CACHE_BOUNDARY + "D")
    control = transport.calls[0]["body"]["system"][0]["cache_control"]
    assert control == {"type": "ephemeral", "ttl": "1h"}


def test_anthropic_invalid_ttl_not_sent():
    transport = Transport([(200, anthropic_body())])
    provider = anthropic_provider(transport, cache_ttl="2d")
    provider.invoke("m", "S" + CACHE_BOUNDARY + "D")
    assert "ttl" not in \
        transport.calls[0]["body"]["system"][0]["cache_control"]


def test_anthropic_no_marker_no_cache_fields():
    transport = Transport([(200, anthropic_body())])
    provider = anthropic_provider(transport)
    provider.invoke("m", "plain prompt with no boundary")
    assert "cache_control" not in json.dumps(transport.calls[0]["body"])


def test_anthropic_cache_disabled_per_provider():
    transport = Transport([(200, anthropic_body())])
    provider = anthropic_provider(transport, cache_enabled=False)
    provider.invoke("m", "S" + CACHE_BOUNDARY + "D")
    assert "cache_control" not in json.dumps(transport.calls[0]["body"])


def test_anthropic_reads_cache_usage_only_when_reported():
    transport = Transport([(200, anthropic_body(cache_read=30))])
    response = anthropic_provider(transport).invoke("m", "p")
    assert response["usage"]["cached_tokens"] == 30
    transport2 = Transport([(200, json.dumps(
        {"content": [{"type": "text", "text": "x"}],
         "stop_reason": "end_turn", "usage": {}}))])
    response2 = anthropic_provider(transport2).invoke("m", "p")
    assert response2["usage"]["cached_tokens"] == 0
    assert "cache_creation_tokens" not in response2["usage"]


# -- openai adapter ------------------------------------------------------------------

def test_openai_prefix_structured_no_cache_fields():
    from providers.openai import OpenAIProvider
    transport = Transport([(200, oai_body("ok"))])
    provider = OpenAIProvider("openai", {"api_key_env": "K"},
                              transport=transport, env={"K": "x"})
    provider.invoke("m", "STABLE" + CACHE_BOUNDARY + "DYNAMIC")
    payload = transport.calls[0]["body"]
    assert payload["messages"][0] == {"role": "system", "content": "STABLE"}
    assert payload["messages"][1]["content"] == "DYNAMIC"
    dumped = json.dumps(payload)
    assert "cache_control" not in dumped and "CACHE-BOUNDARY" not in dumped


# -- CLI honesty --------------------------------------------------------------------

def test_cli_prompt_stripped_and_no_cache_claim(sandbox):
    from providers.cli_codex import CodexCLIBackend
    runner = FakeRunner([
        {"exit_code": 0,
         "stdout": json.dumps({"type": "agent_message",
                               "message": "done"})}])
    backend = CodexCLIBackend("codex", {"binary": "codex"}, runner=runner,
                              which=lambda b: "codex")
    result = backend.invoke("coder", "S" + CACHE_BOUNDARY + "D", None,
                            str(sandbox["repo"]), "read", 60)
    stdin = runner.calls[0]["stdin"]
    assert "CACHE-BOUNDARY" not in stdin
    assert "S" in stdin and "D" in stdin
    # usage carries no invented cache numbers
    assert not result["usage"].get("cached_input_tokens")


# -- telemetry -----------------------------------------------------------------------

def test_package_summary_distinguishes_estimates():
    package = build_package()
    summary = package.summary()
    assert summary["measurement"] == "estimated"
    assert summary["candidate_total_tokens"] >= summary["token_estimate"] \
        - summary["reserved_output_tokens"]
    assert "tokens_by_category" in summary
    assert summary["tokens_by_category"]["policy"] > 0
    assert summary["estimated_savings_tokens"] >= 0
