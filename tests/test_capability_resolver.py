"""Phase 5 -- Autonomous Capability Resolver: search order, deterministic
(never popularity-only) ranking, risk-gated acquisition, alternatives
fallback, escalation policy, bounded search/attempts, and real
integration with the existing skill registry and MCP gateway (no new
authority granted -- their own enable/review/trust state is respected
exactly as-is). No network call is ever made by default."""
import shutil

import pytest

from conftest import AGENTIC_SRC
from core.capability import load_taxonomy
from core.capability.graph import build_graph
from core.capability.requirements import analyse_requirements
from core.capability.resolver import (MAX_RESOLUTION_ATTEMPTS, RegistryCache,
                                      is_safe_to_acquire, preview_candidates,
                                      rank_candidates, resolve_capability,
                                      resolve_project)
from core.projectspec import parse_project_spec
from core.schema import load_schema, validate

TAXONOMY = load_taxonomy(agentic_dir=AGENTIC_SRC, strict=True)

DOCKER_MD = """---
project_type: web_application
---
## Product Vision

An app.

## Functional Requirements

- Run the app in Docker.
"""

ACCESSIBLE_SITE_MD = """---
project_type: web_application
---
## Product Vision

An accessible web app.

## Functional Requirements

- The site must be accessible via keyboard and screen reader.
"""


def _graph_for(md, project_id="proj"):
    spec = parse_project_spec(md)
    plan = analyse_requirements(spec, TAXONOMY, project_id=project_id)
    return build_graph(spec, plan, TAXONOMY, project_id=project_id)


@pytest.fixture
def real_skill_registry(tmp_path):
    from core.skillreg import SkillRegistry
    agentic = tmp_path / "agentic"
    shutil.copytree(AGENTIC_SRC / "skills", agentic / "skills")
    cfg = {"skills": {"enabled": True, "auto_install": False,
                      "allow_scripts": False, "max_injected": 5}}
    return SkillRegistry(cfg, str(agentic))


@pytest.fixture
def real_mcp_gateway(tmp_path):
    from core.mcp import MCPGateway
    cfg = {}
    return MCPGateway(cfg, str(tmp_path / "home"))


# -- individual search sources ---------------------------------------------------------

def test_deterministic_tool_search_finds_docker():
    graph = _graph_for(DOCKER_MD)
    assert graph.state_of("cap:docker") == "unresolved"
    decision = resolve_capability("cap:docker", graph, TAXONOMY)
    assert decision["ok"] is True
    assert decision["chosen"]["type"] == "deterministic_tool"
    assert graph.state_of("cap:docker") == "available"


def test_agent_competence_resolves_capabilities_with_no_suggestions():
    """automated_testing has no suggested_skills/mcp/plugins/
    deterministic_tools in the taxonomy -- it must resolve via ordinary
    agent competence."""
    graph = _graph_for(DOCKER_MD)   # web_application -> automated_testing mandatory
    assert "cap:automated_testing" in graph.nodes
    decision = resolve_capability("cap:automated_testing", graph, TAXONOMY)
    assert decision["ok"] is True
    assert decision["chosen"]["type"] == "agent_competence"


def test_agent_competence_not_offered_when_taxonomy_suggests_a_skill():
    cap_def = TAXONOMY.get("accessibility")
    from core.capability.resolver import _search_agent_competence
    assert _search_agent_competence(cap_def) == []


def test_installed_skill_search_finds_real_builtin_accessibility_skill(
        real_skill_registry):
    graph = _graph_for(ACCESSIBLE_SITE_MD)
    decision = resolve_capability("cap:accessibility", graph, TAXONOMY,
                                  skill_registry=real_skill_registry)
    assert decision["ok"] is True
    assert decision["chosen"]["type"] == "skill"
    assert decision["chosen"]["name"] == "accessibility-review"
    edges = graph.edges_from("cap:accessibility",
                             "capability_satisfied_by_skill")
    assert edges and edges[0]["to"] == "skill:accessibility-review"


def test_installed_mcp_search_finds_enabled_reviewed_server(
        real_mcp_gateway):
    # transactional_email has suggested_mcp_capabilities=[email_provider]
    # and no deterministic_tools/suggested_skills -- isolates the MCP
    # search path with nothing else able to win
    server = real_mcp_gateway.add("email_provider",
                                  command="npx -y @foo/mcp-email",
                                  read_only=True)
    real_mcp_gateway.mark_reviewed(server["id"])
    real_mcp_gateway.enable(server["id"])
    graph = _graph_for(DOCKER_MD)
    graph.add_node("cap:transactional_email", "capability",
                   "Transactional Email",
                   capability_id="transactional_email", mandatory=False,
                   confidence=0.5)
    decision = resolve_capability("cap:transactional_email", graph,
                                  TAXONOMY, mcp_gateway=real_mcp_gateway)
    assert decision["ok"] is True
    assert decision["chosen"]["type"] == "mcp_tool"


def test_installed_mcp_unauthenticated_server_is_rejected(real_mcp_gateway):
    real_mcp_gateway.add("supabase", command="npx -y @supabase/mcp-server",
                         read_only=True, authentication_type="oauth")
    real_mcp_gateway.mark_reviewed("supabase")
    real_mcp_gateway.enable("supabase")
    graph = _graph_for("""---
project_type: web_application
---
## Product Vision

A SaaS app.

## Functional Requirements

- Use Supabase for authentication and the database.
""")
    decision = resolve_capability("cap:supabase", graph, TAXONOMY,
                                  mcp_gateway=real_mcp_gateway)
    # deterministic_tool (supabasex) still resolves it, but the MCP
    # candidate itself must show up as unavailable/rejected, not chosen
    assert all(c["type"] != "mcp_tool" or c["status"] == "unavailable"
              for c in decision["candidates"])


# -- ranking: never popularity-only --------------------------------------------------

def test_resolution_candidate_has_no_popularity_field():
    from core.capability.resolver import _candidate
    c = _candidate("x", "skill", "src", "name")
    assert "popularity" not in c


def test_rank_candidates_orders_by_real_signals_not_insertion_order():
    from core.capability.resolver import _candidate
    weak = _candidate("x", "skill", "s", "weak", trust=0.2,
                      quality_score=0.2, maintenance_score=0.2)
    strong = _candidate("x", "skill", "s", "strong", trust=0.9,
                        quality_score=0.9, maintenance_score=0.9)
    ranked = rank_candidates([weak, strong])
    assert ranked[0]["name"] == "strong"
    assert ranked[0]["evaluation_score"] > ranked[1]["evaluation_score"]


def test_rank_candidates_prefers_instruction_only_skill_over_plugin_at_tie():
    from core.capability.resolver import _candidate
    skill = _candidate("x", "skill", "s", "a", trust=0.5, quality_score=0.5,
                       maintenance_score=0.5)
    plugin = _candidate("x", "plugin_component", "s", "a", trust=0.5,
                        quality_score=0.5, maintenance_score=0.5)
    ranked = rank_candidates([plugin, skill])
    assert ranked[0]["type"] == "skill"


def test_higher_risk_never_beats_lower_risk_at_equal_other_scores():
    from core.capability.resolver import _candidate
    safe = _candidate("x", "skill", "s", "safe", trust=0.5,
                      quality_score=0.5, maintenance_score=0.5, risk="low")
    risky = _candidate("x", "skill", "s", "risky", trust=0.5,
                       quality_score=0.5, maintenance_score=0.5,
                       risk="high")
    ranked = rank_candidates([risky, safe])
    assert ranked[0]["name"] == "safe"


# -- risk gating ------------------------------------------------------------------------

def test_is_safe_to_acquire_blocks_high_risk_capability_regardless():
    from core.capability.resolver import _candidate
    c = _candidate("x", "skill", "s", "n", risk="low", status="available")
    assert is_safe_to_acquire(c, "high") is False
    assert is_safe_to_acquire(c, "low") is True


def test_is_safe_to_acquire_blocks_candidate_riskier_than_capability():
    from core.capability.resolver import _candidate
    c = _candidate("x", "skill", "s", "n", risk="high", status="available")
    assert is_safe_to_acquire(c, "low") is False


def test_unavailable_candidate_never_safe():
    from core.capability.resolver import _candidate
    c = _candidate("x", "skill", "s", "n", risk="low", status="unavailable")
    assert is_safe_to_acquire(c, "low") is False


# -- escalation policy: mandatory blocks, optional continues -------------------------

def test_mandatory_capability_with_no_resolution_is_blocked_and_escalates():
    graph = _graph_for(DOCKER_MD)
    # payments has no deterministic_tools/agent-competence fallback path
    # and is high risk -- force it into the graph as mandatory with no
    # skill/mcp registry available to prove the block/escalate path
    graph.add_node("cap:payments", "capability", "Payments",
                   capability_id="payments", mandatory=True, confidence=0.9)
    decision = resolve_capability("cap:payments", graph, TAXONOMY)
    assert decision["ok"] is False
    assert decision["escalate"] is True
    assert graph.state_of("cap:payments") == "blocked"
    assert graph.get_node("cap:payments")["attributes"]["state_reason"]


def test_optional_capability_with_no_resolution_continues_without_escalating():
    graph = _graph_for(DOCKER_MD)
    graph.add_node("cap:payments", "capability", "Payments",
                   capability_id="payments", mandatory=False, confidence=0.5)
    decision = resolve_capability("cap:payments", graph, TAXONOMY)
    assert decision["ok"] is False
    assert decision["escalate"] is False
    assert graph.state_of("cap:payments") == "unresolved"


# -- alternatives fallback ----------------------------------------------------------

def test_alternative_capability_used_when_direct_candidate_fails():
    import core.capability.taxonomy as taxonomy_mod
    # "a" is high risk -> is_safe_to_acquire() always rejects it, forcing
    # fallback to "b" (its declared alternative), which has no
    # suggestions at all and so resolves via plain agent competence
    stub = taxonomy_mod.Taxonomy(1, ["testcat"], {
        "a": {"id": "a", "name": "A", "category": "testcat",
             "risk_level": "high", "version": 1, "alternatives": ["b"]},
        "b": {"id": "b", "name": "B", "category": "testcat",
             "risk_level": "low", "version": 1, "alternatives": []},
    })
    graph = _graph_for(DOCKER_MD)
    graph.add_node("cap:a", "capability", "A", capability_id="a",
                   mandatory=True, confidence=0.9)
    graph.add_node("cap:b", "capability", "B", capability_id="b",
                   mandatory=False, confidence=0.5)
    decision = resolve_capability("cap:a", graph, stub)
    assert decision["ok"] is True
    assert decision.get("alternative_used") == "b"
    assert graph.state_of("cap:a") == "waived"
    assert graph.state_of("cap:b") == "available"


def test_mutual_alternative_cycle_never_infinite_loops():
    graph = _graph_for(DOCKER_MD)
    # both nodes present, neither has any resolvable candidate source
    # (blank taxonomy stand-ins with a manufactured cyclic alternative)
    import core.capability.taxonomy as taxonomy_mod
    cyclic = taxonomy_mod.Taxonomy(1, ["testcat"], {
        "a": {"id": "a", "name": "A", "category": "testcat",
             "risk_level": "high", "version": 1, "alternatives": ["b"]},
        "b": {"id": "b", "name": "B", "category": "testcat",
             "risk_level": "high", "version": 1, "alternatives": ["a"]},
    })
    graph.add_node("cap:a", "capability", "A", capability_id="a",
                   mandatory=True, confidence=0.9)
    graph.add_node("cap:b", "capability", "B", capability_id="b",
                   mandatory=True, confidence=0.9)
    decision = resolve_capability("cap:a", graph, cyclic)
    assert decision["ok"] is False   # never hangs, never crashes


# -- bounded attempts -----------------------------------------------------------------

def test_resolution_attempts_are_bounded():
    graph = _graph_for(DOCKER_MD)
    graph.add_node("cap:payments", "capability", "Payments",
                   capability_id="payments", mandatory=True, confidence=0.9)
    for _ in range(MAX_RESOLUTION_ATTEMPTS):
        resolve_capability("cap:payments", graph, TAXONOMY)
    final = resolve_capability("cap:payments", graph, TAXONOMY)
    assert "maximum resolution attempts" in final["reason"]


# -- registry cache -------------------------------------------------------------------

def test_registry_cache_returns_none_when_empty():
    cache = RegistryCache()
    assert cache.get("x") is None


def test_registry_cache_expires_after_ttl():
    from conftest import Clock
    clock = Clock()
    cache = RegistryCache(ttl_seconds=100, clock=lambda: clock.now)
    cache.put("x", ["candidate"])
    assert cache.get("x") == ["candidate"]
    clock.now = clock.now.replace(hour=23)   # far beyond ttl same day
    assert cache.get("x") is None


def test_external_registry_hook_never_called_by_default():
    graph = _graph_for(DOCKER_MD)
    calls = []

    def spy_registry_search(cap_def):
        calls.append(cap_def["id"])
        return []
    resolve_project(graph, TAXONOMY)   # no registry_search passed
    assert calls == []


def test_external_registry_hook_used_and_cached_when_supplied():
    from core.capability.resolver import _candidate
    graph = _graph_for(DOCKER_MD)
    calls = []

    def fake_search(cap_def):
        calls.append(cap_def["id"])
        return [{"source": "fake_registry", "name": "fake-tool", "risk": "low",
                "trust": 0.9, "quality_score": 0.9, "maintenance_score": 0.9,
                "status": "available"}]
    cache = RegistryCache()
    graph.add_node("cap:payments", "capability", "Payments",
                   capability_id="payments", mandatory=False, confidence=0.5)
    resolve_capability("cap:payments", graph, TAXONOMY,
                       registry_search=fake_search, cache=cache)
    assert calls == ["payments"]
    # second resolution attempt of the SAME capability re-uses the cache
    resolve_capability("cap:payments", graph, TAXONOMY,
                       registry_search=fake_search, cache=cache)
    assert calls == ["payments"]   # not called again -- cache hit


# -- preview (no mutation) -------------------------------------------------------------

def test_preview_candidates_never_mutates_state():
    graph = _graph_for(DOCKER_MD)
    before = graph.state_of("cap:docker")
    candidates = preview_candidates("docker", TAXONOMY)
    assert graph.state_of("cap:docker") == before == "unresolved"
    assert any(c["type"] == "deterministic_tool" for c in candidates)


# -- resolve_project summary -----------------------------------------------------------

def test_resolve_project_summarises_resolved_and_escalated():
    graph = _graph_for(DOCKER_MD)
    graph.add_node("cap:payments", "capability", "Payments",
                   capability_id="payments", mandatory=True, confidence=0.9)
    summary = resolve_project(graph, TAXONOMY)
    assert summary["resolved_count"] >= 1
    assert any(e["capability_id"] == "payments" for e in
              summary["escalations"])


# -- schema + no-network guard --------------------------------------------------------

def test_candidate_matches_schema():
    from core.capability.resolver import _candidate
    schema = load_schema(str(AGENTIC_SRC / "schemas" /
                             "resolution-candidate.schema.json"))
    c = _candidate("docker", "deterministic_tool", "platform", "dockerx")
    c["evaluation_score"] = 0.5
    assert validate(c, schema) == []


def test_rejected_candidates_are_recorded_with_reasons():
    graph = _graph_for(ACCESSIBLE_SITE_MD)
    graph.get_node("cap:accessibility")["attributes"]["risk_level"] = "low"
    # force an unsafe candidate to appear alongside the safe one
    from core.capability import resolver as resolver_mod
    original = resolver_mod._search_agent_competence
    try:
        def with_bad_candidate(cap_def):
            from core.capability.resolver import _candidate
            return [_candidate(cap_def["id"], "skill", "s", "risky-thing",
                               risk="high", status="available")]
        resolver_mod._search_agent_competence = with_bad_candidate
        decision = resolve_capability("cap:accessibility", graph, TAXONOMY)
        assert any(r["name"] == "risky-thing" and r["rejection_reason"]
                  for r in decision["rejected"])
    finally:
        resolver_mod._search_agent_competence = original


# -- projectops / CLI wiring -----------------------------------------------------------

def test_projectops_resolve_status_candidates_retry_round_trip(
        tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_HOME", str(tmp_path / "home"))
    import core.config as config_mod
    agentic = tmp_path / "agentic"
    shutil.copytree(AGENTIC_SRC / "skills", agentic / "skills")
    shutil.copytree(AGENTIC_SRC / "capabilities", agentic / "capabilities")
    shutil.copytree(AGENTIC_SRC / "schemas", agentic / "schemas")
    monkeypatch.setattr(config_mod, "AGENTIC_DIR", agentic)

    from core.registry import ProjectRegistry
    from core import projectops
    registry = ProjectRegistry(home=str(tmp_path / "home"))
    root = tmp_path / "apps" / "docker-app"
    root.mkdir(parents=True)
    (root / "plan.md").write_text(DOCKER_MD, encoding="utf-8")
    record = registry.add("docker-app", str(root))
    registry.ensure_runtime_dirs(record["id"])

    cfg = {}
    result = projectops.resolve_capabilities(cfg, registry, record)
    assert result is not None
    assert result["summary"]["resolved_count"] >= 1

    status = {cid: n["attributes"]["state"] for cid, n in
             result["graph"].nodes_of_type("capability").items()}
    assert status.get("cap:docker") == "available"

    candidates = projectops.preview_capability_candidates(
        cfg, registry, record, "docker")
    assert any(c["type"] == "deterministic_tool" for c in candidates)

    retry_decision = projectops.retry_capability(cfg, registry, record,
                                                 "docker")
    assert retry_decision["ok"] is True
