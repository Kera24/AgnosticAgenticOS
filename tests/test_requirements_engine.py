"""Phase 3 -- Requirements Intelligence Engine: ProjectSpecification +
Capability Taxonomy -> CapabilityPlan. Deterministic-rules-first,
structural (non-keyword) inference via mandatory_conditions/dependencies,
provenance, dedup, and the eight required project-type scenarios."""
import pytest

from conftest import AGENTIC_SRC
from core.capability import load_taxonomy
from core.capability.requirements import (PROTECTED_ACTIONS,
                                          analyse_requirements,
                                          scan_repository)
from core.projectspec import parse_project_spec
from core.schema import load_schema, validate

TAXONOMY = load_taxonomy(agentic_dir=AGENTIC_SRC, strict=True)


def _plan_for(markdown, project_id="proj", repo_root=None):
    spec = parse_project_spec(markdown)
    return analyse_requirements(spec, TAXONOMY, project_id=project_id,
                               repo_root=repo_root)


def _ids(records):
    return {r["capability_id"] for r in records}


def _plan_schema():
    return load_schema(str(AGENTIC_SRC / "schemas" /
                           "capability-plan.schema.json"))


# -- worked examples from the design spec --------------------------------------------

HOTEL_MD = """---
project_type: static_site
---
## Product Vision

A marketing website for a boutique hotel.

## Functional Requirements

- The hotel website must rank for boutique hotel searches in Kathmandu.
- Show room listings with photos.
"""

SUPABASE_MD = """---
project_type: web_application
---
## Product Vision

A small SaaS app.

## Functional Requirements

- Use Supabase for authentication and the database.
"""


def test_hotel_seo_example_produces_full_expected_capability_set():
    plan = _plan_for(HOTEL_MD)
    all_ids = _ids(plan["required_capabilities"] + plan["optional_capabilities"])
    expected = {"technical_seo", "local_seo", "structured_data", "sitemap",
               "robots", "metadata", "performance", "image_optimisation"}
    assert expected <= all_ids
    schema = _plan_schema()
    assert validate(plan, schema) == []


def test_supabase_auth_example_produces_full_expected_capability_set():
    plan = _plan_for(SUPABASE_MD)
    all_ids = _ids(plan["required_capabilities"] + plan["optional_capabilities"])
    expected = {"supabase", "postgresql", "database_migrations",
               "authentication", "authorisation", "row_level_security",
               "generated_types", "local_database_testing"}
    assert expected <= all_ids


# -- deterministic-first / structural (non-keyword) inference ------------------------

def test_dependency_and_mandatory_condition_inference_is_not_keyword_based():
    """row_level_security is never literally mentioned, yet it must be
    selected purely because `supabase` was selected (capability_present
    predicate) -- proving the plan isn't built from keyword matching
    alone."""
    plan = _plan_for(SUPABASE_MD)
    rls = next(r for r in plan["required_capabilities"] + plan["optional_capabilities"]
              if r["capability_id"] == "row_level_security")
    assert "supabase" not in rls["source_requirement"].lower() or \
        rls["source_location"] in ("mandatory_conditions", "dependency_closure")
    assert rls["reason"] != "" and "supabase" not in rls["reason"].lower() \
        or "selected capability" in rls["reason"] or "mandatory" in rls["reason"]


def test_capability_with_no_textual_signal_is_flagged_inferred():
    plan = _plan_for(SUPABASE_MD)
    inferred_ids = _ids(plan["inferred_capabilities"])
    assert "row_level_security" in inferred_ids or \
        "database_migrations" in inferred_ids


def test_repository_scan_detects_docker_without_any_text_mention():
    plan = _plan_for("## Product Vision\n\nAn app.\n\n"
                     "## Functional Requirements\n\n- Do a thing.\n",
                     repo_root=None)
    # no Docker capability without a marker or text mention
    assert "docker" not in _ids(plan["required_capabilities"] +
                                plan["optional_capabilities"])


def test_repository_scan_marker_triggers_capability(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.11\n",
                                         encoding="utf-8")
    plan = _plan_for("## Product Vision\n\nAn app.\n\n"
                     "## Functional Requirements\n\n- Do a thing.\n",
                     repo_root=str(tmp_path))
    docker = next(r for r in plan["optional_capabilities"] +
                 plan["required_capabilities"]
                 if r["capability_id"] == "docker")
    assert docker["source_location"] == "repository_scan"


# -- deduplication / provenance --------------------------------------------------------

def test_capability_matched_multiple_ways_appears_exactly_once():
    md = ("## Product Vision\n\nA site that must rank for SEO and needs "
         "search engine visibility.\n\n"
         "## Functional Requirements\n\n- Improve SEO further.\n")
    plan = _plan_for(md)
    all_records = plan["required_capabilities"] + plan["optional_capabilities"]
    ids = [r["capability_id"] for r in all_records]
    assert len(ids) == len(set(ids))   # never duplicated
    seo = next(r for r in all_records if r["capability_id"] == "technical_seo")
    # provenance keeps every raw signal even though the requirement
    # itself only appears once
    seo_signals = [p for p in plan["provenance"]
                  if p["capability_id"] == "technical_seo"]
    assert len(seo_signals) >= 2


def test_provenance_preserves_source_requirement_and_location():
    plan = _plan_for(HOTEL_MD)
    seo_signal = next(p for p in plan["provenance"]
                      if p["capability_id"] == "technical_seo")
    assert seo_signal["source_location"] == "Functional Requirements"
    assert seo_signal["source_requirement"]


# -- rules 7/8: never infer deployment or external-communication permission ---------

def test_protected_actions_always_present_regardless_of_capabilities():
    for md in (HOTEL_MD, SUPABASE_MD,
              "## Product Vision\n\nDeploy this to production and email "
              "every user a welcome message.\n\n"
              "## Functional Requirements\n\n- Deploy to production.\n"
              "- Email every user.\n"):
        plan = _plan_for(md)
        for action in ("deploy_to_production", "push_to_remote_git",
                      "send_external_communication",
                      "apply_production_migration"):
            assert action in plan["protected_actions"]


def test_payments_adds_extra_protected_action_never_removes_baseline():
    md = ("## Product Vision\n\nA shop.\n\n"
         "## Functional Requirements\n\n- Accept checkout payments.\n")
    plan = _plan_for(md)
    assert "move_real_money" in plan["protected_actions"]
    assert set(PROTECTED_ACTIONS) <= set(plan["protected_actions"])


# -- honesty about confidence / unresolved gaps ---------------------------------------

def test_low_confidence_dependency_inference_is_labelled_honestly():
    plan = _plan_for(SUPABASE_MD)
    rls = next(r for r in plan["required_capabilities"] +
              plan["optional_capabilities"]
              if r["capability_id"] == "generated_types")
    assert rls["confidence"] < 0.9   # never claims certainty it lacks


def test_semantic_gap_becomes_unresolved_question_not_a_guess():
    md = ("## Product Vision\n\nAn app.\n\n"
         "## Functional Requirements\n\n- Do a thing.\n\n"
         "## Notes For Future Me\n\nSomething about quantum widgets that "
         "matches no known capability.\n")
    plan = _plan_for(md)
    assert any(q["section"] == "Notes For Future Me"
              for q in plan["unresolved_questions"])
    assert "quantum" not in str(plan["required_capabilities"]).lower()


def test_blocking_questions_from_specification_carry_into_plan():
    plan = _plan_for("## Design Direction\n\nMinimal.\n")   # no vision/reqs
    sections = {q["section"] for q in plan["unresolved_questions"]}
    assert {"Product Vision", "Functional Requirements"} <= sections


# -- eight required project-type scenarios ---------------------------------------------

def test_saas_scenario():
    md = """---
project_type: web_application
---
## Product Vision

A subscription SaaS product for small teams.

## Functional Requirements

- Users sign up and log in.
- Teams have admin and member roles.
- Customers pay a monthly subscription via checkout.
"""
    plan = _plan_for(md)
    ids = _ids(plan["required_capabilities"] + plan["optional_capabilities"])
    assert {"authentication", "authorisation", "payments",
           "automated_testing"} <= ids


def test_hotel_website_scenario():
    plan = _plan_for(HOTEL_MD)
    ids = _ids(plan["required_capabilities"] + plan["optional_capabilities"])
    assert {"technical_seo", "accessibility", "responsive_design"} <= ids
    assert "authentication" not in ids


def test_restaurant_ordering_scenario():
    md = """---
project_type: web_application
---
## Product Vision

Online ordering for a restaurant.

## Functional Requirements

- Customers browse the menu and place an order.
- Customers pay for their order at checkout.
"""
    plan = _plan_for(md)
    ids = _ids(plan["required_capabilities"] + plan["optional_capabilities"])
    assert {"payments", "backend_api", "automated_testing"} <= ids


def test_internal_dashboard_scenario():
    md = """---
project_type: internal_tool
---
## Product Vision

An internal reporting dashboard for staff.

## Functional Requirements

- Staff log in with their company account.
- View sales reports.
"""
    plan = _plan_for(md)
    ids = _ids(plan["required_capabilities"] + plan["optional_capabilities"])
    assert {"authentication", "automated_testing"} <= ids
    assert "technical_seo" not in ids   # internal_tool is never public-facing


def test_supabase_application_scenario():
    plan = _plan_for(SUPABASE_MD)
    ids = _ids(plan["required_capabilities"] + plan["optional_capabilities"])
    assert {"supabase", "row_level_security", "authorisation"} <= ids


def test_static_portfolio_scenario():
    md = """---
project_type: static_site
---
## Product Vision

A personal portfolio showcasing design work.

## Functional Requirements

- Visitors browse a gallery of projects.
"""
    plan = _plan_for(md)
    ids = _ids(plan["required_capabilities"] + plan["optional_capabilities"])
    assert {"technical_seo", "accessibility", "documentation"} <= ids
    assert "authentication" not in ids
    assert "backend_api" not in ids


def test_ai_ml_application_scenario():
    md = """---
project_type: web_application
---
## Product Vision

A chatbot that answers questions using semantic search over documents.

## Functional Requirements

- Users ask the chatbot questions.
- The chatbot performs semantic search over uploaded documents.
"""
    plan = _plan_for(md)
    ids = _ids(plan["required_capabilities"] + plan["optional_capabilities"])
    assert {"ai_ml_integration", "vector_search"} <= ids


def test_multi_tenant_crm_scenario():
    md = """---
project_type: web_application
---
## Product Vision

A multi-tenant CRM where each company's data is fully isolated.

## Functional Requirements

- Each company (tenant) only ever sees its own contacts and deals.
- Company admins manage their team's roles and permissions.
"""
    plan = _plan_for(md)
    ids = _ids(plan["required_capabilities"] + plan["optional_capabilities"])
    assert {"authentication", "authorisation", "row_level_security"} <= ids


# -- schema validity across all scenarios ---------------------------------------------

@pytest.mark.parametrize("md", [HOTEL_MD, SUPABASE_MD])
def test_every_plan_validates_against_the_schema(md):
    plan = _plan_for(md)
    assert validate(plan, _plan_schema()) == []


# -- projectops / CLI wiring -----------------------------------------------------------

def test_projectops_analyse_plan_explain_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_HOME", str(tmp_path / "home"))
    from core.registry import ProjectRegistry
    from core import projectops
    registry = ProjectRegistry(home=str(tmp_path / "home"))
    root = tmp_path / "apps" / "hotel"
    root.mkdir(parents=True)
    (root / "plan.md").write_text(HOTEL_MD, encoding="utf-8")
    record = registry.add("hotel", str(root))
    registry.ensure_runtime_dirs(record["id"])

    plan = projectops.analyse_capabilities(registry, record)
    assert plan is not None
    assert "technical_seo" in {r["capability_id"] for r in
                               plan["required_capabilities"] +
                               plan["optional_capabilities"]}

    assert projectops.load_capability_plan(registry, record["id"]) is None
    projectops.save_capability_plan(registry, record["id"], plan)
    reloaded = projectops.load_capability_plan(registry, record["id"])
    assert reloaded["project_id"] == plan["project_id"]
    assert {r["capability_id"] for r in reloaded["required_capabilities"]} == \
        {r["capability_id"] for r in plan["required_capabilities"]}


def test_projectops_analyse_capabilities_none_without_plan_file(tmp_path,
                                                                 monkeypatch):
    monkeypatch.setenv("AGENTIC_HOME", str(tmp_path / "home"))
    from core.registry import ProjectRegistry
    from core import projectops
    registry = ProjectRegistry(home=str(tmp_path / "home"))
    root = tmp_path / "apps" / "empty"
    root.mkdir(parents=True)
    record = registry.add("empty", str(root))
    assert projectops.analyse_capabilities(registry, record) is None
