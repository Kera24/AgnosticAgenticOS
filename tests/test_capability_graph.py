"""Phase 4 -- Capability Graph: persistent, deterministically rebuildable
graph over requirements/capabilities/agent roles/tests/evidence/
acceptance criteria. Satisfaction state machine, waiver reasons, and the
rule that a model's bare claim can never mark a capability satisfied."""
import pytest

from conftest import AGENTIC_SRC
from core.capability import load_taxonomy
from core.capability.graph import (CapabilityGraph, GraphError, build_graph,
                                   load_graph, rebuild_graph, save_graph)
from core.capability.requirements import analyse_requirements
from core.projectspec import parse_project_spec
from core.schema import load_schema, validate

TAXONOMY = load_taxonomy(agentic_dir=AGENTIC_SRC, strict=True)

HOTEL_MD = """---
project_type: static_site
---
## Product Vision

A marketing website for a boutique hotel.

## Functional Requirements

- The hotel website must rank for boutique hotel searches in Kathmandu.
- Show room listings with photos.

## Acceptance Criteria

- The site ranks for "boutique hotel Kathmandu" related queries.
- Room listings render with photos.
"""

SUPABASE_MD = """---
project_type: web_application
---
## Product Vision

A small SaaS app.

## Functional Requirements

- Use Supabase for authentication and the database.
"""


def _graph_for(md, project_id="proj"):
    spec = parse_project_spec(md)
    plan = analyse_requirements(spec, TAXONOMY, project_id=project_id)
    return spec, plan, build_graph(spec, plan, TAXONOMY, project_id=project_id)


# -- node/edge construction -------------------------------------------------------------

def test_hotel_graph_has_expected_node_types():
    spec, plan, graph = _graph_for(HOTEL_MD)
    assert graph.nodes_of_type("capability")
    assert graph.nodes_of_type("requirement")
    assert graph.nodes_of_type("agent_role")
    assert graph.nodes_of_type("test")
    assert graph.nodes_of_type("evidence")
    assert graph.nodes_of_type("acceptance_criterion")
    assert "cap:technical_seo" in graph.nodes
    assert graph.get_node("cap:technical_seo")["attributes"]["category"] \
        == "SEO"


def test_requirement_edges_preserve_provenance():
    spec, plan, graph = _graph_for(HOTEL_MD)
    edges = graph.edges_to("cap:technical_seo",
                           "requirement_requires_capability")
    assert edges
    req_node = graph.get_node(edges[0]["from"])
    assert req_node["type"] == "requirement"
    assert req_node["attributes"]["source_location"] == \
        "Functional Requirements"
    assert edges[0]["attributes"]["reason"]


def test_dependency_edges_from_capability_plan():
    spec, plan, graph = _graph_for(HOTEL_MD)
    deps = {e["to"] for e in graph.edges_from("cap:technical_seo",
                                              "capability_depends_on")}
    assert deps == {"cap:sitemap", "cap:robots", "cap:metadata"}


def test_conflict_edges_created_when_plan_has_conflicts():
    # construct a plan with a synthetic conflict to verify the edge, since
    # the built-in taxonomy has none triggered by these two example specs
    spec, plan, graph = _graph_for(HOTEL_MD)
    plan = dict(plan)
    plan["required_capabilities"] = list(plan["required_capabilities"])
    plan["conflicts"] = [{"capability_id": "technical_seo",
                          "conflicts_with": "sitemap",
                          "resolution": "kept technical_seo"}]
    graph2 = build_graph(spec, plan, TAXONOMY, project_id="proj")
    conflict_edges = graph2.edges_from("cap:technical_seo",
                                       "capability_conflicts_with")
    assert any(e["to"] == "cap:sitemap" for e in conflict_edges)


def test_alternative_edges_from_taxonomy():
    spec, plan, graph = _graph_for(SUPABASE_MD)
    alt_edges = graph.edges_from("cap:supabase", "capability_alternative_to")
    assert any(e["to"] == "cap:relational_database" for e in alt_edges)


def test_agent_role_assignment_edges():
    spec, plan, graph = _graph_for(SUPABASE_MD)
    role_edges = graph.edges_from("cap:authentication",
                                  "capability_assigned_to_agent")
    assert {e["to"] for e in role_edges} == {"agent:coder", "agent:security"}
    assert graph.get_node("agent:coder")["type"] == "agent_role"


def test_project_stated_acceptance_criteria_win_over_taxonomy_baseline():
    spec, plan, graph = _graph_for(HOTEL_MD)
    criteria = [n["label"] for nid, n in
               graph.nodes_of_type("acceptance_criterion").items()
               if nid.startswith("criterion:technical_seo:")]
    assert any("boutique hotel Kathmandu" in c for c in criteria)


def test_check_and_evidence_chain_links_to_acceptance_criteria():
    spec, plan, graph = _graph_for(HOTEL_MD)
    test_ids = {e["to"] for e in graph.edges_from(
        "cap:technical_seo", "capability_validated_by_check")}
    assert test_ids
    evidence_ids = set()
    for tid in test_ids:
        evidence_ids |= {e["to"] for e in
                         graph.edges_from(tid, "check_produces_evidence")}
    assert evidence_ids
    criterion_ids = set()
    for eid in evidence_ids:
        criterion_ids |= {e["to"] for e in graph.edges_from(
            eid, "evidence_satisfies_acceptance_criterion")}
    assert criterion_ids


# -- unknown type / dangling edge guards -------------------------------------------------

def test_add_node_rejects_unknown_type():
    graph = CapabilityGraph("p")
    with pytest.raises(GraphError):
        graph.add_node("x", "not_a_real_type", "X")


def test_add_edge_rejects_unknown_type():
    graph = CapabilityGraph("p")
    graph.add_node("a", "capability", "A")
    graph.add_node("b", "capability", "B")
    with pytest.raises(GraphError):
        graph.add_edge("a", "b", "not_a_real_edge_type")


def test_add_edge_rejects_dangling_reference():
    graph = CapabilityGraph("p")
    graph.add_node("a", "capability", "A")
    with pytest.raises(GraphError):
        graph.add_edge("a", "missing", "capability_depends_on")


# -- satisfaction state machine ----------------------------------------------------------

def test_new_capabilities_start_unresolved():
    spec, plan, graph = _graph_for(HOTEL_MD)
    assert graph.state_of("cap:technical_seo") == "unresolved"
    assert "cap:technical_seo" in graph.unresolved_capabilities()


def test_set_state_rejects_unknown_state():
    graph = CapabilityGraph("p")
    graph.add_node("cap:a", "capability", "A")
    with pytest.raises(GraphError):
        graph.set_state("cap:a", "definitely_done")


def test_waiver_requires_a_reason():
    graph = CapabilityGraph("p")
    graph.add_node("cap:a", "capability", "A")
    with pytest.raises(GraphError):
        graph.set_state("cap:a", "waived")
    graph.set_state("cap:a", "waived", reason="explicitly out of scope")
    assert graph.state_of("cap:a") == "waived"
    assert graph.get_node("cap:a")["attributes"]["state_reason"] == \
        "explicitly out of scope"


def test_mark_satisfied_rejects_model_claim_alone():
    spec, plan, graph = _graph_for(HOTEL_MD)
    graph.record_evidence("cap:technical_seo", "I believe this is done",
                          source="model_claim")
    with pytest.raises(GraphError):
        graph.mark_satisfied("cap:technical_seo")
    assert graph.state_of("cap:technical_seo") == "unresolved"


def test_mark_satisfied_accepts_verified_evidence():
    spec, plan, graph = _graph_for(HOTEL_MD)
    graph.record_evidence("cap:technical_seo",
                          "sitemap.xml present and valid",
                          source="deterministic_check")
    graph.mark_satisfied("cap:technical_seo")
    assert graph.state_of("cap:technical_seo") == "satisfied"
    assert "cap:technical_seo" not in graph.unresolved_capabilities()


@pytest.mark.parametrize("source", ["test_run", "human_review",
                                    "reviewer_agent"])
def test_mark_satisfied_accepts_every_verified_source(source):
    spec, plan, graph = _graph_for(HOTEL_MD)
    graph.record_evidence("cap:technical_seo", "verified", source=source)
    graph.mark_satisfied("cap:technical_seo")
    assert graph.state_of("cap:technical_seo") == "satisfied"


def test_mark_satisfied_ignores_expected_placeholder_evidence():
    """The `build_graph`-created "expected evidence" nodes (source
    "expected", not yet recorded) must never count toward satisfaction
    on their own -- only real, recorded evidence does."""
    spec, plan, graph = _graph_for(HOTEL_MD)
    with pytest.raises(GraphError):
        graph.mark_satisfied("cap:technical_seo")


# -- determinism / rebuild --------------------------------------------------------------

def test_rebuild_is_identical_to_build():
    spec, plan, graph1 = _graph_for(HOTEL_MD)
    graph2 = rebuild_graph(spec, plan, TAXONOMY, project_id="proj")
    assert graph1.to_dict() == graph2.to_dict()


def test_graph_matches_schema():
    spec, plan, graph = _graph_for(SUPABASE_MD)
    schema = load_schema(str(AGENTIC_SRC / "schemas" /
                             "capability-graph.schema.json"))
    assert validate(graph.to_dict(), schema) == []


# -- persistence (project-isolated) ------------------------------------------------------

def test_save_and_load_round_trip(tmp_path):
    spec, plan, graph = _graph_for(HOTEL_MD, project_id="proj-a")
    save_graph(tmp_path, graph)
    reloaded = load_graph(tmp_path)
    assert reloaded.to_dict() == graph.to_dict()


def test_load_graph_returns_none_when_absent(tmp_path):
    assert load_graph(tmp_path) is None


def test_two_projects_never_share_graph_storage(tmp_path):
    spec, plan, graph_a = _graph_for(HOTEL_MD, project_id="proj-a")
    spec_b, plan_b, graph_b = _graph_for(SUPABASE_MD, project_id="proj-b")
    save_graph(tmp_path / "a", graph_a)
    save_graph(tmp_path / "b", graph_b)
    reloaded_a = load_graph(tmp_path / "a")
    reloaded_b = load_graph(tmp_path / "b")
    assert reloaded_a.project_id == "proj-a"
    assert reloaded_b.project_id == "proj-b"
    assert "cap:supabase" not in reloaded_a.nodes
    assert "cap:technical_seo" not in reloaded_b.nodes


# -- projectops / CLI wiring -----------------------------------------------------------

def test_projectops_build_save_load_graph_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_HOME", str(tmp_path / "home"))
    from core.registry import ProjectRegistry
    from core import projectops
    registry = ProjectRegistry(home=str(tmp_path / "home"))
    root = tmp_path / "apps" / "hotel"
    root.mkdir(parents=True)
    (root / "plan.md").write_text(HOTEL_MD, encoding="utf-8")
    record = registry.add("hotel", str(root))
    registry.ensure_runtime_dirs(record["id"])

    assert projectops.load_capability_graph(registry, record["id"]) is None
    graph = projectops.build_capability_graph(registry, record)
    assert graph is not None
    projectops.save_capability_graph(registry, record["id"], graph)
    reloaded = projectops.load_capability_graph(registry, record["id"])
    assert reloaded.to_dict() == graph.to_dict()
