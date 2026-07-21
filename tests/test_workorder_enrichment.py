"""Phase 10 -- Capability-Aware Planning and Execution: `enrich_work_order`,
`record_capability_evidence`, `confirm_ready_for_dispatch`, the small set
of real deterministic capability checks, and end-to-end wiring through
the live cycle loop (`core.project.run_cycle`)."""
from conftest import (AGENTIC_SRC, Clock, FakeCaller, project_cfg,
                      proj_order, seed_project, simple_task, verifier_out,
                      worker_out)
from core import projstate
from core.capability import load_taxonomy
from core.capability.checks import run_capability_checks, run_check
from core.capability.graph import build_graph, load_graph, save_graph
from core.capability.predispatch import confirm_ready_for_dispatch
from core.capability.requirements import analyse_requirements
from core.capability.workorder import (enrich_work_order,
                                       record_capability_evidence)
from core.mcp import MCPError
from core.modelcap import ModelCapabilityRegistry, model_record
from core.project import run_cycle
from core.projectspec import parse_project_spec

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


def _graph_for(md, project_id="proj"):
    spec = parse_project_spec(md)
    plan = analyse_requirements(spec, TAXONOMY, project_id=project_id)
    return spec, plan, build_graph(spec, plan, TAXONOMY,
                                   project_id=project_id)


def _order(**over):
    order = {"item": "improve SEO rankings", "spec": "", "objective": "",
            "risk": "low"}
    order.update(over)
    return order


# -- enrich_work_order ----------------------------------------------------------------

def test_enrich_adds_required_capabilities_from_order_text():
    _, _, graph = _graph_for(HOTEL_MD)
    enriched = enrich_work_order(_order(), {"id": "t1"}, graph=graph,
                                 taxonomy=TAXONOMY)
    assert "technical_seo" in enriched["required_capabilities"]
    assert enriched["task_id"] == "t1"


def test_enrich_only_matches_capabilities_already_in_the_plan():
    _, _, graph = _graph_for(HOTEL_MD)
    # "payments" is a real taxonomy capability but was never selected for
    # this project's plan -- it must never leak into required_capabilities
    enriched = enrich_work_order(
        _order(item="add a payments checkout flow"), {"id": "t1"},
        graph=graph, taxonomy=TAXONOMY)
    assert "payments" not in enriched["required_capabilities"]


def test_enrich_reads_already_resolved_skill_selection_from_graph():
    _, _, graph = _graph_for(HOTEL_MD)
    graph.add_node("skill:seo-optimization", "skill", "seo-optimization")
    graph.add_edge("cap:technical_seo", "skill:seo-optimization",
                  "capability_satisfied_by_skill", evaluation_score=0.8)
    enriched = enrich_work_order(_order(), {"id": "t1"}, graph=graph,
                                 taxonomy=TAXONOMY)
    assert "seo-optimization" in enriched["selected_skills"]


def test_enrich_evidence_requirements_from_taxonomy():
    _, _, graph = _graph_for(HOTEL_MD)
    enriched = enrich_work_order(_order(), {"id": "t1"}, graph=graph,
                                 taxonomy=TAXONOMY)
    assert "sitemap.xml" in enriched["evidence_requirements"]


def test_enrich_protected_actions_from_capability_plan():
    _, plan, graph = _graph_for(HOTEL_MD)
    enriched = enrich_work_order(_order(), {"id": "t1"}, graph=graph,
                                 taxonomy=TAXONOMY, capability_plan=plan)
    assert "deploy_to_production" in enriched["protected_actions"]


def test_enrich_sets_selected_agent_role_from_role_param():
    _, _, graph = _graph_for(HOTEL_MD)
    enriched = enrich_work_order(_order(), {"id": "t1"}, graph=graph,
                                 taxonomy=TAXONOMY, role="coder")
    assert enriched["selected_agent_role"] == "coder"


def test_enrich_never_overrides_a_conductor_supplied_field():
    _, _, graph = _graph_for(HOTEL_MD)
    enriched = enrich_work_order(
        _order(required_capabilities=["custom_value"]), {"id": "t1"},
        graph=graph, taxonomy=TAXONOMY)
    assert enriched["required_capabilities"] == ["custom_value"]


def test_enrich_never_mutates_the_input_order():
    _, _, graph = _graph_for(HOTEL_MD)
    order = _order()
    before = dict(order)
    enrich_work_order(order, {"id": "t1"}, graph=graph, taxonomy=TAXONOMY)
    assert order == before


def test_enrich_degrades_to_empty_defaults_without_a_graph():
    enriched = enrich_work_order(_order(), {"id": "t1"})
    assert enriched["required_capabilities"] == []
    assert enriched["selected_skills"] == []
    assert enriched["selected_backend"] is None


def test_enrich_selects_backend_and_model_from_registry():
    registry = ModelCapabilityRegistry([
        model_record(backend="codex", provider="codex", model_id="gpt-5",
                    available=True, reasoning_class="medium",
                    historical_success=0.9)])
    enriched = enrich_work_order(_order(), {"id": "t1"}, role="coder",
                                 model_registry=registry, cfg={})
    assert enriched["selected_backend"] == "codex"
    assert enriched["selected_model"] == "gpt-5"


def test_enrich_leaves_backend_none_when_nothing_available():
    registry = ModelCapabilityRegistry([])
    enriched = enrich_work_order(_order(), {"id": "t1"}, role="coder",
                                 model_registry=registry, cfg={})
    assert enriched["selected_backend"] is None
    assert enriched["selected_model"] is None


# -- record_capability_evidence --------------------------------------------------------

def test_record_evidence_marks_capability_satisfied_when_gate_passes():
    _, _, graph = _graph_for(HOTEL_MD)
    recorded = record_capability_evidence(graph, ["technical_seo"],
                                          task_id="t1", gate_ok=True)
    assert recorded == ["technical_seo"]
    assert graph.state_of("cap:technical_seo") == "satisfied"


def test_record_evidence_does_nothing_when_gate_failed():
    _, _, graph = _graph_for(HOTEL_MD)
    recorded = record_capability_evidence(graph, ["technical_seo"],
                                          task_id="t1", gate_ok=False)
    assert recorded == []
    assert graph.state_of("cap:technical_seo") != "satisfied"


def test_record_evidence_ignores_unknown_capability_id():
    _, _, graph = _graph_for(HOTEL_MD)
    recorded = record_capability_evidence(graph, ["not_a_real_capability"],
                                          task_id="t1", gate_ok=True)
    assert recorded == []


def test_record_evidence_handles_none_graph():
    assert record_capability_evidence(None, ["technical_seo"], task_id="t1",
                                      gate_ok=True) == []


# -- confirm_ready_for_dispatch ---------------------------------------------------------

def test_predispatch_warns_on_unresolved_mandatory_capability():
    _, _, graph = _graph_for(HOTEL_MD)   # technical_seo starts "unresolved"
    order = {"required_capabilities": ["technical_seo"],
             "selected_skills": [], "selected_mcp_tools": [],
             "allowed_paths": []}
    ok, warnings = confirm_ready_for_dispatch(order, graph=graph)
    assert not ok
    assert any("technical_seo" in w for w in warnings)


def test_predispatch_ok_when_capability_available():
    _, _, graph = _graph_for(HOTEL_MD)
    graph.set_state("cap:technical_seo", "available")
    order = {"required_capabilities": ["technical_seo"],
             "selected_skills": [], "selected_mcp_tools": [],
             "allowed_paths": []}
    ok, warnings = confirm_ready_for_dispatch(order, graph=graph)
    assert ok and warnings == []


class _FakeSkillRegistry:
    def __init__(self, verified):
        self.verified = verified

    def verify(self, skill_id):
        return self.verified.get(skill_id, {"ok": False,
                                             "reason": "missing"})


def test_predispatch_warns_on_failed_skill_verification():
    order = {"selected_skills": ["bad-skill"], "selected_mcp_tools": [],
             "allowed_paths": []}
    ok, warnings = confirm_ready_for_dispatch(
        order, skill_registry=_FakeSkillRegistry({}))
    assert not ok
    assert any("bad-skill" in w for w in warnings)


def test_predispatch_ok_when_skill_verifies():
    order = {"selected_skills": ["good-skill"], "selected_mcp_tools": [],
             "allowed_paths": []}
    ok, warnings = confirm_ready_for_dispatch(
        order, skill_registry=_FakeSkillRegistry(
            {"good-skill": {"ok": True}}))
    assert ok and warnings == []


class _FakeMCPGateway:
    def __init__(self, records):
        self.records = records

    def get(self, server_id):
        if server_id not in self.records:
            raise MCPError("unknown server %r" % server_id)
        return self.records[server_id]


def test_predispatch_warns_on_unconfigured_mcp_server():
    order = {"selected_skills": [], "selected_mcp_tools": ["ghost"],
             "allowed_paths": []}
    ok, warnings = confirm_ready_for_dispatch(
        order, mcp_gateway=_FakeMCPGateway({}))
    assert not ok
    assert any("ghost" in w for w in warnings)


def test_predispatch_warns_on_disabled_mcp_server():
    order = {"selected_skills": [], "selected_mcp_tools": ["srv"],
             "allowed_paths": []}
    ok, warnings = confirm_ready_for_dispatch(
        order, mcp_gateway=_FakeMCPGateway(
            {"srv": {"enabled": False, "reviewed": True}}))
    assert not ok


def test_predispatch_warns_on_unavailable_backend():
    registry = ModelCapabilityRegistry([
        model_record(backend="codex", provider="codex", model_id="gpt-5",
                    available=False, reasoning_class="medium")])
    order = {"selected_skills": [], "selected_mcp_tools": [],
             "selected_backend": "codex", "allowed_paths": []}
    ok, warnings = confirm_ready_for_dispatch(order, model_registry=registry)
    assert not ok


def test_predispatch_warns_on_protected_path():
    order = {"selected_skills": [], "selected_mcp_tools": [],
             "allowed_paths": ["supabase/migrations/x.sql"]}
    ok, warnings = confirm_ready_for_dispatch(
        order, protected=["supabase/migrations/**"])
    assert not ok


def test_predispatch_all_clear_returns_ok_true():
    ok, warnings = confirm_ready_for_dispatch(
        {"selected_skills": [], "selected_mcp_tools": [],
         "allowed_paths": []})
    assert ok and warnings == []


# -- real deterministic capability checks ------------------------------------------------

def test_sitemap_and_robots_present(tmp_path):
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "sitemap.xml").write_text("<urlset/>",
                                                      encoding="utf-8")
    (tmp_path / "robots.txt").write_text("User-agent: *", encoding="utf-8")
    results = run_capability_checks(
        ["sitemap_present", "robots_present"], str(tmp_path))
    by_name = {r["name"]: r for r in results}
    assert by_name["sitemap_present"]["passed"] is True
    assert by_name["robots_present"]["passed"] is True
    assert all(r["implemented"] for r in results)


def test_sitemap_missing_fails(tmp_path):
    implemented, passed, _detail = run_check("sitemap_present", str(tmp_path))
    assert implemented and passed is False


def test_meta_titles_present_requires_every_html_file_to_have_a_title(
        tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><title>Home</title></html>", encoding="utf-8")
    (tmp_path / "about.html").write_text(
        "<html><body>no title</body></html>", encoding="utf-8")
    implemented, passed, _detail = run_check("meta_titles_present",
                                             str(tmp_path))
    assert implemented and passed is False


def test_readme_present(tmp_path):
    (tmp_path / "README.md").write_text("# hi", encoding="utf-8")
    implemented, passed, _detail = run_check("readme_present", str(tmp_path))
    assert implemented and passed is True


def test_unimplemented_check_reported_honestly(tmp_path):
    implemented, passed, _detail = run_check(
        "migration_reproducible_locally", str(tmp_path))
    assert implemented is False
    assert passed is None


# -- end-to-end wiring through the live cycle loop ---------------------------------------

def _seed_capability_state(sandbox, *, with_skill_edge=True):
    a = str(sandbox["agentic"])
    _spec, plan, graph = _graph_for(HOTEL_MD, project_id="proj")
    if with_skill_edge:
        graph.add_node("skill:seo-optimization", "skill", "seo-optimization")
        graph.add_edge("cap:technical_seo", "skill:seo-optimization",
                      "capability_satisfied_by_skill", evaluation_score=0.8)
    projstate.write_yaml(a, "capability-plan.yaml", plan)
    save_graph(a, graph)
    return plan, graph


_SEC_PASS = {"verdict": "pass", "concerns": [], "reason": "clean"}


def test_cycle_enriches_work_order_and_records_evidence(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    _seed_capability_state(sandbox)
    order = proj_order(task, item="improve SEO rankings for the site",
                       spec="add sitemap and robots")
    caller = FakeCaller({"conductor": order, "coder": worker_out(),
                         "qa": verifier_out("pass"), "security": _SEC_PASS})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock(),
                       run_id="c1")
    assert result["status"] == "success"

    coder_call = [c for c in caller.calls if c["role"] == "coder"][0]
    wo = coder_call["input"]["work_order"]
    assert "technical_seo" in wo["required_capabilities"]
    assert "seo-optimization" in wo["selected_skills"]
    assert wo["selected_agent_role"] == "coder"
    assert wo["task_id"] == task["id"]
    assert "sitemap.xml" in wo["evidence_requirements"]

    graph_after = load_graph(str(sandbox["agentic"]))
    assert graph_after.state_of("cap:technical_seo") == "satisfied"


def test_cycle_succeeds_despite_predispatch_warnings_advisory_only(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    # deliberately do NOT resolve technical_seo -- it stays "unresolved",
    # which the pre-dispatch check flags as a warning
    _seed_capability_state(sandbox, with_skill_edge=False)
    order = proj_order(task, item="improve SEO rankings for the site")
    caller = FakeCaller({"conductor": order, "coder": worker_out(),
                         "qa": verifier_out("pass"), "security": _SEC_PASS})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock(),
                       run_id="c1")
    assert result["status"] == "success"


def test_cycle_without_capability_plan_leaves_work_order_unaffected(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    caller = FakeCaller({"conductor": proj_order(task),
                         "coder": worker_out(), "qa": verifier_out("pass"),
                         "security": _SEC_PASS})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock(),
                       run_id="c1")
    assert result["status"] == "success"
    coder_call = [c for c in caller.calls if c["role"] == "coder"][0]
    wo = coder_call["input"]["work_order"]
    assert "required_capabilities" not in wo
    assert "selected_skills" not in wo
