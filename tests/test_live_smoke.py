"""OPT-IN live smoke tests. Skipped unless AGENTIC_LIVE_SMOKE=1.

These make REAL calls: CLI backends consume subscription quota and API
backends cost money. They are excluded from the default suite on purpose —
run explicitly with:

    AGENTIC_LIVE_SMOKE=1 python -m pytest tests/test_live_smoke.py -q
"""
import os

import pytest

LIVE = os.environ.get("AGENTIC_LIVE_SMOKE") == "1"
pytestmark = pytest.mark.skipif(
    not LIVE, reason="live smoke tests are opt-in (set AGENTIC_LIVE_SMOKE=1); "
                     "they consume real quota")


def test_live_detected_backends_smoke():
    from core.config import load_config
    from core.setupwiz import detect_backends
    from core import backends as backends_mod
    from core.config import repo_root

    cfg = load_config()
    detected, _apis = detect_backends(cfg)
    if not detected:
        pytest.skip("no CLI/local backends installed on this machine")
    results = {}
    for name in detected:
        adapter = backends_mod.build_backend(cfg, name)
        results[name] = adapter.smoke_test(str(repo_root(cfg)))
    assert any(results.values()), "no live backend passed its smoke test: %r" \
        % results
