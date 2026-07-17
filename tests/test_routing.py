"""Phase 6 — capability routing: chain ordering, exclusions, breaker/auth
handling, reviewer independence, rebuilt fallback context, decisions."""
import json
import os

from conftest import Clock
from core.breaker import BreakerBoard
from core.routing import (backend_capabilities, capability_chain, discover,
                          read_decisions)


def cap_cfg(agents=None, policies=None, backends=None):
    return {
        "project": {"name": "test"},
        "backends": backends if backends is not None else {
            "claude": {"type": "cli", "kind": "configured",
                       "binary": "claude",
                       "capabilities": {"reasoning": "highest",
                                        "coding": "high",
                                        "review": "high"}},
            "codex": {"type": "cli", "kind": "codex", "binary": "codex",
                      "capabilities": {"coding": "highest",
                                       "reasoning": "high",
                                       "review": "high"}},
            "ollama": {"type": "local", "model": "qwen3.5:latest"},
        },
        "routing": {"mode": "capability",
                    "policies": policies or {},
                    "agents": agents or {}},
    }


def test_capability_ordering_prefers_declared_preferences(tmp_path):
    cfg = cap_cfg(agents={"coder": {
        "capabilities": {"coding": "high"},
        "preferred": [{"backend": "codex", "model": "auto"},
                      {"backend": "claude", "model": "auto"},
                      {"backend": "ollama", "model": "auto"}]}})
    chain = capability_chain(cfg, "coder", memory_dir=str(tmp_path))
    assert chain[0] == "codex"
    # ollama is below the capability bar but explicitly preferred -> kept
    assert set(chain) == {"codex", "claude", "ollama"}


def test_strongest_backend_first_without_preferences(tmp_path):
    cfg = cap_cfg(agents={"architect": {
        "capabilities": {"reasoning": "highest"}}})
    chain = capability_chain(cfg, "architect", memory_dir=str(tmp_path))
    assert chain[0] == "claude"        # highest declared reasoning strength


def test_insufficient_capability_rejected(tmp_path):
    cfg = cap_cfg(agents={"coder": {"capabilities": {"coding": "high"}}})
    chain = capability_chain(cfg, "coder", memory_dir=str(tmp_path))
    assert "ollama" not in chain[:2]   # medium coding sorts/filters below
    decisions = read_decisions(str(tmp_path))
    assert decisions
    rejected = {r["backend"]: r["reason"]
                for r in decisions[-1]["rejected"]}
    assert "ollama" in rejected and "coding" in rejected["ollama"]


def test_embedding_models_never_generative(tmp_path):
    cfg = cap_cfg(backends={
        "ollama": {"type": "local", "model": "nomic-embed-text-v2-moe"},
        "claude": {"type": "cli", "binary": "claude"}})
    chain = capability_chain(cfg, "coder", memory_dir=str(tmp_path))
    assert chain == ["claude"]
    reasons = [r["reason"] for r in
               read_decisions(str(tmp_path))[-1]["rejected"]]
    assert any("embedding" in r for r in reasons)


def test_auth_failure_excluded_never_routed_around(tmp_path):
    board = BreakerBoard(str(tmp_path), clock=Clock())
    board.record_failure("claude", "auth")
    assert board.state("claude") == "authentication_required"
    cfg = cap_cfg()
    chain = capability_chain(cfg, "coder", memory_dir=str(tmp_path),
                             board=board)
    assert "claude" not in chain
    reasons = [r["reason"] for r in
               read_decisions(str(tmp_path))[-1]["rejected"]]
    assert any("authentication" in r for r in reasons)


def test_rate_limited_backend_deprioritized(tmp_path):
    clock = Clock()
    board = BreakerBoard(str(tmp_path), clock=clock)
    board.record_failure("codex", "rate_limit")
    cfg = cap_cfg(agents={"coder": {
        "capabilities": {"coding": "high"},
        "preferred": [{"backend": "codex"}]}})
    chain = capability_chain(cfg, "coder", memory_dir=str(tmp_path),
                             board=board)
    assert chain[0] != "codex"          # cooling backend sorts last
    assert "codex" in chain             # but may recover later


def test_reviewer_independence(tmp_path):
    cfg = cap_cfg(policies={"reviewer_different_from_worker": True})
    worker = capability_chain(cfg, "coder", memory_dir=str(tmp_path))
    review = capability_chain(cfg, "qa", memory_dir=str(tmp_path),
                              worker_chain=worker)
    assert review[0] != worker[0]
    assert worker[0] in review          # still reachable as last resort


def test_reviewer_independence_unsatisfiable_recorded(tmp_path):
    cfg = cap_cfg(policies={"reviewer_different_from_worker": True},
                  backends={"claude": {"type": "cli", "binary": "claude"}})
    review = capability_chain(cfg, "qa", memory_dir=str(tmp_path),
                              worker_chain=["claude"])
    assert review == ["claude"]
    assert read_decisions(str(tmp_path))[-1]["warnings"]


def test_local_fallback_can_be_disallowed(tmp_path):
    cfg = cap_cfg(policies={"allow_local_fallback": False})
    chain = capability_chain(cfg, "coder", memory_dir=str(tmp_path))
    assert "ollama" not in chain[1:]


def test_simple_routing_unchanged(base_cfg):
    from core.backends import routing_chain
    base_cfg["routing"] = {"mode": "simple", "primary": "a",
                           "fallbacks": ["b"]}
    assert routing_chain(base_cfg, "coder") == ["a", "b"]
    base_cfg["routing"] = {"mode": "per_agent", "primary": "a",
                           "per_agent": {"qa": {"primary": "c"}}}
    assert routing_chain(base_cfg, "qa") == ["c"]
    assert routing_chain(base_cfg, "coder") == ["a"]


def test_fallback_context_rebuilt_per_backend(sandbox, monkeypatch):
    """The fallback backend must receive a package built for its own
    (smaller) window — never the primary's prompt."""
    from conftest import project_cfg
    from core import project as project_mod
    from core.breaker import BreakerBoard as BB
    from core.capacity import CapacityLedger
    from core import errors

    cfg = project_cfg(sandbox)
    cfg["context"] = {"default_input_budget_tokens": 4000,
                      "reserved_output_tokens": 200}
    cfg["backends"]["mock"]["context_window"] = 100000
    cfg["backends"]["mock2"]["context_window"] = 2000
    memdir = str(sandbox["agentic"] / "memory")
    prompts = {}

    class FlakyAdapter:
        backend_type = "api"

        def __init__(self, name):
            self.name = name

        def invoke(self, role, prompt, input_data, workspace, permissions,
                   timeout):
            prompts[self.name] = prompt
            if self.name == "mock":
                raise errors.RateLimitError("slow down", provider="mock")
            return {"ok": True, "backend": self.name, "content": "{}",
                    "structured_output": {}, "usage": {}, "refusal": False}

    monkeypatch.setattr(project_mod.backends, "build_backend",
                        lambda cfg_, name, **kw: FlakyAdapter(name))
    caller = project_mod.make_caller(cfg, CapacityLedger(cfg, memdir),
                                     BB(memdir), memory_dir=memdir)
    filler = {"work_order": {"item": "x"},
              "memories": "recorded note line\n" * 3000}   # optional bulk
    result = caller("qa", "Review.", filler)
    assert result["ok"]
    assert result["backend"] == "mock2"
    assert len(prompts["mock2"]) < len(prompts["mock"])
    from core.context.tokenizer import estimate_tokens
    assert estimate_tokens(prompts["mock2"]) <= 2000 - 200


def test_discovery_reports_honestly(tmp_path, base_cfg):
    base_cfg["backends"] = {
        "mock": {"type": "api", "provider": "mock", "model": "m"}}
    report = discover(base_cfg, str(tmp_path))
    entry = report[0]
    assert entry["backend"] == "mock"
    assert entry["capacity_confidence"] == "unknown"
    assert "success_rate_24h" in entry
    assert entry["auth"] in ("ok", "required", "unknown")


def test_backend_capability_defaults():
    assert backend_capabilities({"type": "cli"})["reasoning"] == "high"
    assert backend_capabilities({"type": "local"})["coding"] == "medium"
    caps = backend_capabilities({"type": "local",
                                 "capabilities": {"coding": "high"}})
    assert caps["coding"] == "high"
