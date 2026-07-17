"""Code-intelligence factory with honest fallback (ADR 0002).

get_adapter() resolves context.code_intelligence.provider, health-checks it,
and falls back (provider -> fallback -> none) with the reason recorded in
the returned adapter's `fallback_reason`. The OS never hard-fails because a
retrieval engine is missing.
"""
from .base import CodeIntelligenceAdapter, is_excluded  # noqa: F401
from .cce import CCEAdapter, CCEUnavailable
from .native import NativeAdapter
from .none_adapter import NoneAdapter

DEFAULT_CI_CONFIG = {
    "provider": "native",
    "fallback": "none",
    "index_on_project_start": True,
    "incremental_after_commit": True,
    "search_limit": 12,
    "expansion_limit": 4,
    "excluded_paths": [],
}

_ADAPTERS = {"none": NoneAdapter, "native": NativeAdapter, "cce": CCEAdapter}


def ci_config(cfg):
    raw = ((cfg.get("context") or {}).get("code_intelligence")) or {}
    merged = dict(DEFAULT_CI_CONFIG)
    merged.update(raw)
    return merged


def get_adapter(cfg, project_root, memory_dir, runner=None, which=None):
    """Build the best available adapter. Never raises for a missing engine —
    degrades along provider -> fallback -> none and records why."""
    cicfg = ci_config(cfg)
    order = []
    for name in (cicfg["provider"], cicfg.get("fallback"), "none"):
        if name and name not in order:
            order.append(name)
    reason = None
    for name in order:
        cls = _ADAPTERS.get(name)
        if cls is None:
            reason = "unknown code-intelligence provider %r" % name
            continue
        kwargs = {"cfg": cicfg}
        if name == "cce":
            kwargs.update(runner=runner, which=which)
        try:
            adapter = cls(project_root, memory_dir, **kwargs)
            health = adapter.health_check()
            if health.get("ok"):
                adapter.fallback_reason = reason
                return adapter
            reason = "%s unhealthy: %s" % (name, health.get("detail"))
        except (CCEUnavailable, Exception) as exc:  # noqa: BLE001
            reason = "%s unavailable: %s" % (name, str(exc)[:200])
    adapter = NoneAdapter(project_root, memory_dir, cfg=cicfg)
    adapter.fallback_reason = reason
    return adapter
