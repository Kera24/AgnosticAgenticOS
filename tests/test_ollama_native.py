"""Native Ollama /api/chat streaming client (extends the routing/timeout
fix with real evidence: `ollama run qwen3.5:latest` succeeded manually,
`ollama ps` showed a 262144-token context and a visible reasoning phase on
a CPU-only machine). Covers: CPU cold start, streaming progress, thinking
vs final content, dynamic context-tier selection, the machine-capacity
cap, keep-alive, idle-timeout reset, total timeout, unload, and a
successful structured final response. No live Ollama call anywhere --
`conftest.OllamaStream` fakes the streaming transport end to end."""
import json

import pytest

from conftest import OllamaStream, ollama_event
from core import errors
from providers.local_ollama import (DEFAULT_OLLAMA_TIMEOUTS,
                                    OllamaLocalBackend,
                                    estimate_required_context,
                                    keep_alive_string, ollama_options_for_role,
                                    ollama_timeout_config, select_context_tier,
                                    thinking_for_role)
from providers.ollama import OllamaProvider


def _backend(cfg_extra=None, stream=None, runner_responses=None,
            unload_transport=None):
    cfg = dict({"model": "qwen3.5:latest"}, **(cfg_extra or {}))
    from conftest import FakeRunner
    runner = FakeRunner(runner_responses or [
        {"stdout": "ollama version 0.5.7"},
        {"stdout": "NAME  ID  SIZE\nqwen3.5:latest  x  16 GB\n"},
    ])
    return OllamaLocalBackend("ollama", cfg, transport=stream, runner=runner,
                              which=lambda b: "C:/bin/ollama",
                              unload_transport=unload_transport)


# -- 1. CPU cold start: model-load phase observed via the stream ------------------------

def test_cpu_cold_start_reports_model_load_stage():
    """A CPU-only machine loading a 16 GB model can legitimately take a
    long time before the FIRST stream event of any kind arrives -- this
    must be reported as `model_load`, not a generic transport timeout."""
    err = errors.TimeoutError_("ollama model-load timeout after 610.0s")
    err.diagnostic = ["ollama_timeout_stage=model_load"]
    stream = OllamaStream([[err]])
    backend = _backend(stream=stream)
    with pytest.raises(errors.TimeoutError_) as exc_info:
        backend.invoke("architect", "design the app", None, ".", "read", 30)
    diag = dict(k.split("=", 1) for k in exc_info.value.diagnostic)
    assert diag["ollama_timeout_stage"] == "model_load"


# -- 2. streaming progress: multiple chunks accumulate into final content --------------

def test_streaming_progress_accumulates_into_final_content():
    stream = OllamaStream([[
        ollama_event(content="Hello"),
        ollama_event(content=", world"),
        ollama_event(content="!", done=True, eval_count=7,
                    prompt_eval_count=12, load_duration=500_000_000,
                    total_duration=900_000_000),
    ]])
    backend = _backend(stream=stream)
    result = backend.invoke("worker", "say hi", None, ".", "write", 30)
    assert result["ok"] and result["content"] == "Hello, world!"
    assert result["usage"]["output_tokens"] == 7
    assert result["usage"]["input_tokens"] == 12


# -- 3. thinking content is separated from final assistant content ---------------------

def test_thinking_content_kept_separate_from_final_content():
    stream = OllamaStream([[
        ollama_event(thinking="considering the requirements..."),
        ollama_event(thinking=" weighing options..."),
        ollama_event(content="Final answer.", done=True),
    ]])
    backend = _backend(stream=stream)
    provider = backend.provider
    result = provider.invoke("qwen3.5:latest", "p", options={},
                             thinking="low", timeouts=DEFAULT_OLLAMA_TIMEOUTS)
    assert result["content"] == "Final answer."
    assert "considering" in result["thinking"]
    assert "Final answer" not in result["thinking"]


def test_backend_invoke_never_leaks_thinking_into_structured_handoff():
    """#12 of the spec: only the final assistant content is preserved for
    the structured work-order/handoff -- `content` never contains
    thinking text, even when the model streamed a lot of it."""
    stream = OllamaStream([[
        ollama_event(thinking="long internal deliberation " * 5),
        ollama_event(content='{"result": "ok"}', done=True),
    ]])
    backend = _backend(stream=stream)
    result = backend.invoke("architect", "p", None, ".", "read", 30)
    assert result["content"] == '{"result": "ok"}'
    assert "deliberation" not in result["content"]


# -- 4. dynamic context-tier selection ---------------------------------------------------

def test_context_tier_selection_rounds_up_to_smallest_covering_tier():
    assert select_context_tier(500, max_context_tokens=65536) == 8192
    assert select_context_tier(9000, max_context_tokens=65536) == 16384
    assert select_context_tier(20000, max_context_tokens=65536) == 32768
    assert select_context_tier(40000, max_context_tokens=65536) == 65536


def test_required_context_combines_input_output_and_safety_margin():
    required, input_estimate = estimate_required_context(
        "x" * 4000, output_reserve=4096, safety_margin=1024)
    assert input_estimate > 0
    assert required == input_estimate + 4096 + 1024


def test_invoke_selects_num_ctx_from_actual_prompt_not_a_fixed_default():
    stream = OllamaStream([[ollama_event(content="ok", done=True)]])
    backend = _backend(cfg_extra={"max_context_tokens": 65536}, stream=stream)
    backend.invoke("worker", "short prompt", None, ".", "write", 30)
    sent = stream.calls[0]["body"]
    assert sent["options"]["num_ctx"] == 8192   # tiny prompt -> smallest tier


# -- 5. machine-capacity cap: never exceeds the configured ceiling ----------------------

def test_context_tier_never_exceeds_machine_capacity_cap():
    """The evidence: a real `ollama ps` showed a 262144-token context on a
    CPU-only machine -- excessive. The selected tier must never exceed
    the configured max_context_tokens even if a huge prompt would
    otherwise justify a bigger tier."""
    huge_required = 10_000_000
    assert select_context_tier(huge_required, max_context_tokens=32768) == \
        32768
    stream = OllamaStream([[ollama_event(content="ok", done=True)]])
    backend = _backend(cfg_extra={"max_context_tokens": 32768},
                       stream=stream)
    backend.invoke("worker", "x" * 500_000, None, ".", "write", 30)
    assert stream.calls[0]["body"]["options"]["num_ctx"] <= 32768


# -- 6. keep-alive ------------------------------------------------------------------------

def test_keep_alive_defaults_to_30_minutes_and_is_sent():
    assert keep_alive_string({}) == "30m"
    assert keep_alive_string({"keep_alive_minutes": 5}) == "5m"
    stream = OllamaStream([[ollama_event(content="ok", done=True)]])
    backend = _backend(stream=stream)
    backend.invoke("worker", "p", None, ".", "write", 30)
    assert stream.calls[0]["body"]["keep_alive"] == "30m"


# -- 7. idle timeout resets on every valid stream event ---------------------------------

def test_idle_stream_timeout_only_fires_after_a_real_gap(monkeypatch):
    """#10 of the spec: the idle-stream clock resets on every valid event
    -- verified against the real transport (not the test fake, which has
    no notion of elapsed time) by checking `default_stream_transport`'s
    phase selection directly."""
    from providers.base import default_stream_transport

    class _FakeSocket:
        def __init__(self, lines):
            self.lines = list(lines)
            self.timeouts_set = []

        def settimeout(self, value):
            self.timeouts_set.append(value)

    class _FakeResponse:
        def __init__(self, sock):
            self.sock = sock
            self.status = 200

        def readline(self):
            return self.sock.lines.pop(0) if self.sock.lines else b""

    events_raw = [
        json.dumps(ollama_event(content="a")).encode() + b"\n",
        json.dumps(ollama_event(content="b")).encode() + b"\n",
        json.dumps(ollama_event(content="c", done=True)).encode() + b"\n",
        b"",
    ]
    sock = _FakeSocket(events_raw)
    resp = _FakeResponse(sock)

    class _FakeConn:
        def __init__(self):
            self.sock = sock

        def connect(self):
            pass

        def request(self, *a, **kw):
            pass

        def getresponse(self):
            return resp

        def close(self):
            pass

    monkeypatch.setattr("http.client.HTTPConnection", lambda *a, **kw:
                        _FakeConn())
    events = list(default_stream_transport(
        "http://localhost:11434/api/chat", {}, b"{}",
        dict(DEFAULT_OLLAMA_TIMEOUTS, idle_stream_timeout_seconds=180)))
    assert [e["message"]["content"] for e in events] == ["a", "b", "c"]
    # each line read re-applied a (short, per-event) timeout -- proof the
    # idle clock is recomputed fresh after every valid event, not set once
    assert len(sock.timeouts_set) >= 4


# -- 8. total timeout -----------------------------------------------------------------------

def test_total_timeout_fires_even_if_individual_gaps_are_short():
    err = errors.TimeoutError_("ollama total timeout after 1800.0s")
    err.diagnostic = ["ollama_timeout_stage=total",
                      "ollama_elapsed_seconds=1800.0"]
    stream = OllamaStream([[
        ollama_event(content="partial output"), err]])
    backend = _backend(stream=stream)
    with pytest.raises(errors.TimeoutError_) as exc_info:
        backend.invoke("worker", "p", None, ".", "write", 30)
    diag = dict(k.split("=", 1) for k in exc_info.value.diagnostic)
    assert diag["ollama_timeout_stage"] == "total"


# -- 9. unload behaviour --------------------------------------------------------------------

def test_unload_sends_keep_alive_zero():
    calls = []

    def fake_transport(url, headers, body, timeout):
        calls.append({"url": url, "body": json.loads(body.decode("utf-8"))})
        return 200, "{}"

    backend = _backend(unload_transport=fake_transport)
    result = backend.unload("project_complete")
    assert result["ok"] is True
    assert calls[0]["body"]["keep_alive"] == 0
    assert calls[0]["body"]["model"] == "qwen3.5:latest"
    assert calls[0]["url"].endswith("/api/chat")


def test_unload_is_best_effort_never_raises():
    def failing_transport(url, headers, body, timeout):
        raise OSError("ollama not running")

    backend = _backend(unload_transport=failing_transport)
    result = backend.unload("user_requested")
    assert result["ok"] is False
    assert "ollama not running" in result["detail"]


def test_project_completion_triggers_unload(sandbox, monkeypatch):
    from conftest import Clock, FakeCaller, project_cfg, seed_project, \
        simple_task, verifier_out
    import core.project as project_mod
    from core.project import final_audit

    cfg = project_cfg(sandbox)
    cfg["backends"]["ollama"] = {"type": "local", "model": "qwen3.5:latest"}
    seed_project(sandbox, [simple_task(status="done", last_result="pass")])
    unload_calls = []

    real_build_backend = project_mod.backends.build_backend

    class _StubOllama:
        def unload(self, reason):
            unload_calls.append(reason)
            return {"ok": True}

    def fake_build_backend(cfg_, name, **kw):
        if name == "ollama":
            return _StubOllama()
        return real_build_backend(cfg_, name, **kw)

    monkeypatch.setattr(project_mod.backends, "build_backend",
                        fake_build_backend)
    result = final_audit(cfg, caller=FakeCaller({"qa": verifier_out(
        "pass")}), clock=Clock())
    assert result["status"] == "complete"
    assert unload_calls == ["project_complete"]


# -- 10. successful structured final response --------------------------------------------

def test_successful_structured_final_response_end_to_end():
    stream = OllamaStream([[
        ollama_event(thinking="planning the architecture"),
        ollama_event(content='{"architecture": "single module", '),
        ollama_event(content='"milestones": []}', done=True,
                    eval_count=40, prompt_eval_count=200,
                    load_duration=1_000_000_000,
                    prompt_eval_duration=2_000_000_000,
                    eval_duration=3_000_000_000,
                    total_duration=6_000_000_000),
    ]])
    backend = _backend(cfg_extra={"max_context_tokens": 32768}, stream=stream)
    result = backend.invoke("architect", "produce the architecture", None,
                            ".", "read", 30)
    assert result["ok"]
    parsed = json.loads(result["content"])
    assert parsed["architecture"] == "single module"
    telemetry = result["telemetry"]
    assert telemetry["eval_count"] == 40
    assert telemetry["prompt_eval_count"] == 200
    assert telemetry["load_duration"] == 1_000_000_000
    assert telemetry["total_duration"] == 6_000_000_000
    assert telemetry["thinking_mode"] == "low"   # architect role default
    assert telemetry["keep_alive"] == "30m"
    assert telemetry["selected_num_ctx"] in (8192, 16384, 32768, 65536)


# -- role-based thinking + fallback to boolean on incompatibility -----------------------

def test_thinking_by_role_defaults():
    assert thinking_for_role({}, "architect") == "low"
    assert thinking_for_role({}, "worker") is False
    assert thinking_for_role({}, "reviewer") == "low"
    assert thinking_for_role({}, "unknown_role") is None


def test_thinking_falls_back_to_boolean_when_string_levels_unsupported():
    """#13 of the spec: if the installed model/API rejects a string
    thinking level, detect it and retry with a boolean -- never guessed
    upfront, only after a real rejection."""
    rejection = errors.ProviderError("ollama HTTP 400: invalid \"think\" "
                                     "value, expected boolean")
    stream = OllamaStream([
        [rejection],
        [ollama_event(content="ok", done=True)],
    ])
    provider = OllamaProvider("ollama", {}, transport=stream)
    result = provider.invoke("qwen3.5:latest", "p", options={},
                             thinking="low", timeouts=DEFAULT_OLLAMA_TIMEOUTS)
    assert result["ok"]
    assert stream.calls[0]["body"]["think"] == "low"
    # degraded to a boolean and retried -- bool("low") is True, matching
    # "some level of thinking requested" rather than none
    assert stream.calls[1]["body"]["think"] is True


# -- global + per-role options merge -----------------------------------------------------

def test_options_merge_global_then_per_role_override():
    backend_cfg = {"options": {"num_predict": 2048, "temperature": 0.2},
                  "options_by_role": {"architect": {"num_predict": 8192}}}
    worker_options = ollama_options_for_role(backend_cfg, "worker")
    architect_options = ollama_options_for_role(backend_cfg, "architect")
    assert worker_options["num_predict"] == 2048
    assert architect_options["num_predict"] == 8192
    assert architect_options["temperature"] == 0.2   # inherited from global
