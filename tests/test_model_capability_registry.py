"""Phase 8 -- Model Capability Registry: dynamic discovery, deterministic
classification (backend-type default -> config override -> name
heuristic -> "unknown", never a guessed hardcoded model name), role-alias
resolution with graceful tier degradation, and persistence. No live
provider call -- discovery reuses the same safe version/auth/model-list
probes doctor/setup already make, injectable via FakeRunner exactly like
every other backend test in this suite."""
import pytest

from conftest import FakeRunner
from core.modelcap import (CAPABILITY_CLASSES, ModelCapabilityRegistry,
                           ModelCapError, classify_model, discover_registry,
                           is_embedding_model_id, load_registry,
                           model_record, save_registry)


# -- classification: never a hardcoded model-name assumption ------------------------

def test_cli_backend_defaults_to_frontier():
    cls, reason = classify_model("codex", "cli", None)
    assert cls == "frontier"
    assert "backend type default" in reason


def test_local_backend_defaults_to_medium():
    cls, reason = classify_model("ollama", "local", "qwen3.5:latest")
    assert cls == "medium"


def test_api_backend_with_no_signal_is_unknown_not_guessed():
    """A never-seen-before API model name must never be silently assumed
    to be any particular tier -- this is the "do not assume Fable/GPT-
    5.6 Sol/Opus/Sonnet/Qwen stay available" requirement in practice."""
    cls, reason = classify_model("some_new_provider", "api",
                                 "totally-novel-model-xyz-2099")
    assert cls == "unknown"
    assert "configure" in reason.lower()


@pytest.mark.parametrize("model_id,expected", [
    ("gpt-4o-mini", "lightweight"), ("claude-haiku-9", "lightweight"),
    ("some-flash-model", "lightweight"), ("gemini-nano", "lightweight"),
])
def test_api_name_heuristic_lightweight(model_id, expected):
    cls, reason = classify_model("api_backend", "api", model_id)
    assert cls == expected
    assert "heuristic" in reason


@pytest.mark.parametrize("model_id", ["model-opus-9", "big-72b-model",
                                      "reasoning-large-v2"])
def test_api_name_heuristic_high(model_id):
    cls, reason = classify_model("api_backend", "api", model_id)
    assert cls == "high"


def test_explicit_override_wins_over_backend_type_default():
    overrides = [{"backend": "codex", "class": "high"}]
    cls, reason = classify_model("codex", "cli", None, overrides=overrides)
    assert cls == "high"
    assert "override" in reason


def test_explicit_override_by_model_id_pattern():
    overrides = [{"model_id_pattern": r"^my-frontier-model$",
                 "class": "frontier"}]
    cls, _ = classify_model("api_backend", "api", "my-frontier-model",
                            overrides=overrides)
    assert cls == "frontier"
    cls2, _ = classify_model("api_backend", "api", "some-other-model",
                             overrides=overrides)
    assert cls2 == "unknown"


def test_override_scoped_to_backend_does_not_leak_to_other_backends():
    overrides = [{"backend": "codex", "class": "high"}]
    cls, _ = classify_model("claude", "cli", None, overrides=overrides)
    assert cls == "frontier"   # unaffected, still the cli default


def test_is_embedding_model_id():
    assert is_embedding_model_id("nomic-embed-text-v2-moe:latest")
    assert not is_embedding_model_id("qwen3.5:latest")
    assert not is_embedding_model_id(None)


def test_every_class_is_a_known_tier():
    assert CAPABILITY_CLASSES == ("frontier", "high", "medium",
                                  "lightweight", "unknown")


# -- registry lookups / alias resolution -----------------------------------------------

def _rec(backend, cls, *, available=True, local=False, success=None,
        breaker="available", model_id="m"):
    return model_record(backend=backend, provider=backend, model_id=model_id,
                        available=available, reasoning_class=cls,
                        local=local, historical_success=success,
                        circuit_breaker=breaker)


def test_best_returns_available_record_at_requested_class():
    reg = ModelCapabilityRegistry([_rec("codex", "frontier")])
    best = reg.best("frontier")
    assert best["backend"] == "codex"


def test_best_degrades_to_lower_tier_when_nothing_available_at_top():
    reg = ModelCapabilityRegistry([
        _rec("codex", "frontier", available=False),
        _rec("ollama", "medium", local=True)])
    best = reg.best("frontier")
    assert best["backend"] == "ollama"
    assert best["reasoning_class"] == "medium"


def test_best_returns_none_when_nothing_available_at_all():
    reg = ModelCapabilityRegistry([_rec("codex", "frontier", available=False)])
    assert reg.best("frontier") is None


def test_best_prefers_requested_backend_at_equal_tier():
    reg = ModelCapabilityRegistry([_rec("claude", "frontier"),
                                   _rec("codex", "frontier")])
    best = reg.best("frontier", prefer_backend="codex")
    assert best["backend"] == "codex"


def test_best_avoids_broken_circuit_breaker_when_alternative_exists():
    reg = ModelCapabilityRegistry([
        _rec("codex", "frontier", breaker="unavailable"),
        _rec("claude", "frontier", breaker="available")])
    best = reg.best("frontier")
    assert best["backend"] == "claude"


def test_best_prefers_higher_historical_success():
    reg = ModelCapabilityRegistry([
        _rec("codex", "frontier", success=0.5),
        _rec("claude", "frontier", success=0.95)])
    best = reg.best("frontier")
    assert best["backend"] == "claude"


def test_resolve_alias_role_prefix_is_informational_same_class_pool():
    reg = ModelCapabilityRegistry([_rec("codex", "frontier")])
    a = reg.resolve_alias("orchestrator_frontier")
    b = reg.resolve_alias("architect_frontier")
    assert a["backend"] == b["backend"] == "codex"


def test_resolve_alias_local_fallback_ignores_class_prefers_local():
    reg = ModelCapabilityRegistry([_rec("codex", "frontier"),
                                   _rec("ollama", "medium", local=True)])
    result = reg.resolve_alias("local_fallback")
    assert result["backend"] == "ollama"
    assert result["local"] is True


def test_resolve_alias_malformed_raises():
    reg = ModelCapabilityRegistry([])
    with pytest.raises(ModelCapError):
        reg.resolve_alias("not_a_real_alias_at_all")


def test_resolve_alias_returns_none_not_exception_when_nothing_available():
    reg = ModelCapabilityRegistry([_rec("codex", "frontier",
                                        available=False)])
    assert reg.resolve_alias("coder_medium") is None


# -- discovery: assembled from injected detection layers, no live provider call ----

def test_discover_registry_from_injected_detection_data():
    cfg = {"backends": {"codex": {"type": "cli"},
                        "ollama": {"type": "local"}}}
    detected = {
        "codex": {"installed": True, "version": "codex-cli 1.0",
                  "models": []},
        "ollama": {"installed": True, "version": "ollama 0.5",
                  "models": ["qwen3.5:latest",
                           "nomic-embed-text-v2-moe:latest"]},
    }
    auth_reports = {
        "codex": {"state": "authenticated",
                  "smoke_test": {"ok": True}},
        "ollama": {"state": "local_ok"},
    }
    registry = discover_registry(cfg, detected=detected, apis={},
                                 auth_reports=auth_reports)
    ids = {(r["backend"], r["model_id"]) for r in registry.records}
    assert ("codex", "provider_default") in ids
    assert ("ollama", "qwen3.5:latest") in ids
    # embedding models are never surfaced as a generative capability
    assert ("ollama", "nomic-embed-text-v2-moe:latest") not in ids
    codex_rec = next(r for r in registry.records if r["backend"] == "codex")
    assert codex_rec["reasoning_class"] == "frontier"
    assert codex_rec["available"] is True
    assert codex_rec["smoke_tested"] is True


def test_discover_registry_marks_unauthenticated_backend_unavailable():
    cfg = {"backends": {"codex": {"type": "cli"}}}
    detected = {"codex": {"installed": True, "version": "v1", "models": []}}
    auth_reports = {"codex": {"state": "not_authenticated"}}
    registry = discover_registry(cfg, detected=detected, apis={},
                                 auth_reports=auth_reports)
    assert registry.records[0]["available"] is False


def test_discover_registry_never_reprobes_when_data_injected(monkeypatch):
    """Doctor already calls detect_backends/backend_auth_report once;
    the registry must not silently probe again."""
    import core.setupwiz as setupwiz_mod
    import core.authx as authx_mod

    def boom(*a, **k):
        raise AssertionError("should not re-probe when data is injected")
    monkeypatch.setattr(setupwiz_mod, "detect_backends", boom)
    monkeypatch.setattr(authx_mod, "backend_auth_report", boom)
    discover_registry({"backends": {}}, memory_dir="/nonexistent",
                      detected={}, apis={}, auth_reports={})


def test_discover_registry_api_backend_uses_configured_model():
    cfg = {"backends": {"my_api": {"type": "api", "model": "some-mini-model"}}}
    apis = {"my_api": {"configured": True, "api_key_env": "X"}}
    registry = discover_registry(cfg, detected={}, apis=apis,
                                 auth_reports={})
    rec = registry.records[0]
    assert rec["model_id"] == "some-mini-model"
    assert rec["reasoning_class"] == "lightweight"   # "mini" heuristic
    assert rec["available"] is True


# -- persistence ----------------------------------------------------------------------

def test_save_and_load_registry_round_trip(tmp_path):
    registry = ModelCapabilityRegistry([_rec("codex", "frontier")])
    save_registry(str(tmp_path), registry)
    reloaded = load_registry(str(tmp_path))
    assert reloaded.to_dict() == registry.to_dict()


def test_load_registry_returns_none_when_absent(tmp_path):
    assert load_registry(str(tmp_path)) is None


# -- doctor integration -----------------------------------------------------------------

def test_doctor_reports_model_capability_registry(sandbox, monkeypatch):
    import core.doctor as doctor_mod
    monkeypatch.setattr(doctor_mod, "AGENTIC_DIR", sandbox["agentic"])
    ok, checks = doctor_mod.run_doctor(cfg=sandbox["cfg"])
    assert any(msg.startswith("model capability registry")
              for _level, msg in checks)


def test_doctor_run_persists_the_registry(sandbox, monkeypatch):
    import core.doctor as doctor_mod
    from core.modelcap import load_registry
    # doctor.py binds AGENTIC_DIR at import time; patch doctor's own name
    # (see test_capability_taxonomy.py for the same pattern) so this test
    # never touches the real platform's memory directory.
    monkeypatch.setattr(doctor_mod, "AGENTIC_DIR", sandbox["agentic"])
    memory = str(sandbox["agentic"] / "memory")
    doctor_mod.run_doctor(cfg=sandbox["cfg"])
    assert load_registry(memory) is not None
