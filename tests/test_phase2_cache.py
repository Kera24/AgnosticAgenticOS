"""Phase 2 -- Built-in Prompt and Context Cache: the artifact/result
cache store, its wiring into the stable-policy-prefix path, honest
provider-native telemetry labelling, and the CLI surface."""
import json
import os

import pytest

from core import cachestore
from core.cachestore import CacheError, CacheStore, classify_provider_cache_report
from core.context import compose as compose_mod


# -- A/B. cache store core: put/get, categories, identity ----------------------

def test_put_get_artifact_round_trips(tmp_path):
    store = CacheStore(str(tmp_path))
    key = cachestore.compute_cache_key(role="coder", repository_revision="r1")
    value, hit = store.get(key)
    assert hit is False and value is None
    store.put_artifact(key, "stable text", "prompt_prefix_text",
                       tokens_estimated=10)
    value, hit = store.get(key)
    assert hit is True and value == "stable text"


def test_compute_cache_key_is_deterministic_and_component_sensitive():
    k1 = cachestore.compute_cache_key(role="coder", model="m1",
                                      repository_revision="abc")
    k2 = cachestore.compute_cache_key(role="coder", model="m1",
                                      repository_revision="abc")
    k3 = cachestore.compute_cache_key(role="coder", model="m1",
                                      repository_revision="def")
    assert k1 == k2
    assert k1 != k3


def test_forbidden_categories_never_cacheable(tmp_path):
    store = CacheStore(str(tmp_path))
    for category in cachestore.FORBIDDEN_CATEGORIES:
        with pytest.raises(CacheError):
            store.put_artifact("k-" + category, "some generated content",
                               category)


def test_result_cache_rejects_non_result_category(tmp_path):
    store = CacheStore(str(tmp_path))
    with pytest.raises(CacheError):
        store.put_result("k1", "text", "prompt_prefix_text")   # artifact-only
    # a genuine result category works fine
    store.put_result("k2", {"pytest": True}, "tooling_detection")
    value, hit = store.get("k2")
    assert hit and value == {"pytest": True}


def test_unknown_category_rejected(tmp_path):
    store = CacheStore(str(tmp_path))
    with pytest.raises(CacheError):
        store.put_artifact("k1", "x", "not_a_real_category")


# -- D. safety: secrets never enter the cache ----------------------------------

def test_secret_looking_content_rejected(tmp_path):
    store = CacheStore(str(tmp_path))
    with pytest.raises(CacheError):
        store.put_artifact("k1", "sk-ant-" + "a" * 20, "prompt_prefix_text")


def test_sensitive_hint_rejected_regardless_of_content(tmp_path):
    store = CacheStore(str(tmp_path))
    with pytest.raises(CacheError):
        store.put_artifact("k1", "totally benign text", "prompt_prefix_text",
                           source_hint="auth-verification.json")


def test_verify_flags_anything_that_slipped_past_write_time(tmp_path):
    store = CacheStore(str(tmp_path))
    store.put_artifact("k1", "fine", "prompt_prefix_text")
    # simulate a stale entry written before a safety-rule update
    path = cachestore._entry_path(str(tmp_path), "k2")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"key": "k2", "category": "prompt_prefix_text",
                  "value": "sk-ant-" + "b" * 20, "dependencies": {},
                  "created_at": "2026-01-01T00:00:00"}, fh)
    result = store.verify()
    assert result["ok"] is False
    assert any(p["key"] == "k2" for p in result["problems"])


# -- C. dependency-aware invalidation -------------------------------------------

def test_invalidate_dependents_only_touches_matching_entries(tmp_path):
    store = CacheStore(str(tmp_path))
    store.put_artifact("k1", "a", "prompt_prefix_text",
                       dependencies={"policy_files": "hash1"})
    store.put_artifact("k2", "b", "prompt_prefix_text",
                       dependencies={"other_dep": "hashX"})
    removed = store.invalidate_dependents("policy_files", "hash2")
    assert removed == ["k1"]
    _, hit1 = store.get("k1")
    _, hit2 = store.get("k2")
    assert hit1 is False   # invalidated
    assert hit2 is True    # untouched -- not a blanket clear


def test_ttl_expiry_prunes_only_expired_entries(tmp_path):
    class FixedClock:
        def __init__(self):
            import datetime as _dt
            self.now = _dt.datetime(2026, 1, 1, 0, 0, 0)

        def __call__(self):
            return self.now

    clock = FixedClock()
    store = CacheStore(str(tmp_path), clock=clock)
    store.put_artifact("short", "x", "prompt_prefix_text", ttl_seconds=60)
    store.put_artifact("long", "y", "prompt_prefix_text", ttl_seconds=None)
    import datetime as _dt
    clock.now += _dt.timedelta(seconds=120)
    removed = store.prune_expired()
    assert removed == ["short"]
    _, hit_long = store.get("long")
    assert hit_long is True


def test_clear_with_category_scopes_removal(tmp_path):
    store = CacheStore(str(tmp_path))
    store.put_artifact("k1", "a", "prompt_prefix_text")
    store.put_result("k2", {}, "tooling_detection")
    store.clear(category="prompt_prefix_text")
    _, hit1 = store.get("k1")
    _, hit2 = store.get("k2")
    assert hit1 is False
    assert hit2 is True


# -- F. honest provider-native telemetry ----------------------------------------

def test_cli_backend_always_provider_cache_unknown():
    status, tokens = classify_provider_cache_report(
        "cli", {"cached_tokens": 500})   # even if present, CLI never claims it
    assert status == "provider_cache_unknown"
    assert tokens is None


def test_api_backend_reports_actual_cached_tokens():
    status, tokens = classify_provider_cache_report(
        "api", {"cached_tokens": 128})
    assert status == "provider_cache_reported"
    assert tokens == 128


def test_api_backend_unknown_when_no_cache_field_present():
    status, tokens = classify_provider_cache_report("api", {"input_tokens": 10})
    assert status == "provider_cache_unknown"
    assert tokens is None


def test_record_provider_cache_observation_persists_and_counts(tmp_path):
    store = CacheStore(str(tmp_path))
    store.record_provider_cache_observation("k1", "api", {"cached_tokens": 50})
    store.record_provider_cache_observation("k2", "cli", {"cached_tokens": 999})
    status = store.status()
    assert status["provider_cache_observations"] == 2
    assert status["provider_cache_reported_count"] == 1   # only the api one


# -- E/exit gate: identical stable contexts reuse cached artifacts -------------

def test_policy_text_cache_hit_on_second_call(tmp_path, monkeypatch):
    from core import config as config_mod
    fake_agentic = tmp_path / "agentic"
    (fake_agentic / "prompts").mkdir(parents=True)
    (fake_agentic / "prompts" / "shared-autonomy.md").write_text(
        "autonomy rules\n", encoding="utf-8")
    (fake_agentic / "prompts" / "shared-scope.md").write_text(
        "scope rules\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "AGENTIC_DIR", fake_agentic)
    memory_dir = str(tmp_path / "memory")

    first = compose_mod._policy_text(memory_dir)
    second = compose_mod._policy_text(memory_dir)
    assert first == second == "autonomy rules\n\n\nscope rules\n"

    telemetry_path = os.path.join(memory_dir, cachestore.TELEMETRY_FILENAME)
    events = [json.loads(line) for line in
             open(telemetry_path, encoding="utf-8")]
    hits = [e for e in events if e["event"] == "application_cache_hit"]
    misses = [e for e in events if e["event"] == "application_cache_miss"]
    assert len(misses) == 1   # first call: nothing cached yet
    assert len(hits) == 1     # second call: reused the cached artifact


def test_policy_text_cache_invalidated_when_file_changes(tmp_path, monkeypatch):
    from core import config as config_mod
    fake_agentic = tmp_path / "agentic"
    (fake_agentic / "prompts").mkdir(parents=True)
    p1 = fake_agentic / "prompts" / "shared-autonomy.md"
    p1.write_text("v1\n", encoding="utf-8")
    (fake_agentic / "prompts" / "shared-scope.md").write_text("scope\n",
                                                               encoding="utf-8")
    monkeypatch.setattr(config_mod, "AGENTIC_DIR", fake_agentic)
    memory_dir = str(tmp_path / "memory")

    first = compose_mod._policy_text(memory_dir)
    p1.write_text("v2 -- changed\n", encoding="utf-8")
    second = compose_mod._policy_text(memory_dir)
    assert first != second
    assert "v2 -- changed" in second


def test_policy_text_never_caches_without_memory_dir(tmp_path, monkeypatch):
    """Backward-compatible fallback: a caller with no memory_dir gets the
    same content, just uncached (no cache directory is ever created)."""
    from core import config as config_mod
    fake_agentic = tmp_path / "agentic"
    (fake_agentic / "prompts").mkdir(parents=True)
    (fake_agentic / "prompts" / "shared-autonomy.md").write_text(
        "x\n", encoding="utf-8")
    (fake_agentic / "prompts" / "shared-scope.md").write_text(
        "y\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "AGENTIC_DIR", fake_agentic)
    text = compose_mod._policy_text(None)
    assert text == "x\n\n\ny\n"
    assert not os.path.exists(str(tmp_path / "memory" / "cache"))


# -- CLI surface -----------------------------------------------------------------

def test_cache_cli_status_and_verify_smoke(tmp_path, monkeypatch):
    import subprocess
    import sys
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run_script = os.path.join(repo, ".agentic", "run")
    proc = subprocess.run([sys.executable, run_script, "cache", "status"],
                          cwd=repo, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert "entries" in data and "hit_rate" in data
    proc2 = subprocess.run([sys.executable, run_script, "cache", "verify"],
                           cwd=repo, capture_output=True, text=True, timeout=30)
    assert proc2.returncode == 0
    assert json.loads(proc2.stdout)["ok"] is True
