"""Phase 13 -- End-to-End Autonomous Fixtures: six mocked, connected
scenarios exercising the full capability-intelligence pipeline through
the REAL call surface (`core.project.run_cycle`/`final_audit`, the same
functions the live scheduler calls) -- spec parsing -> Requirements
Intelligence -> Capability Graph -> Resolver -> work-order enrichment ->
the coder/QA/security cycle -> Completion Contract, and (fixture 6) the
Phase 12 dashboard reading the same persisted state back. No live
provider/network call is ever made; the model-invocation boundary is
the same `caller` substitution point Phase 9-11's tests already use.
"""
import pytest

from conftest import (AGENTIC_SRC, Clock, FakeCaller, project_cfg,
                      proj_order, seed_project, simple_task, verifier_out,
                      worker_out)
from core import projectops, projstate
from core.capability import load_taxonomy
from core.capability.graph import load_graph
from core.project import final_audit, project_start, run_cycle

TAXONOMY = load_taxonomy(agentic_dir=AGENTIC_SRC, strict=True)

_SEC_PASS = {"verdict": "pass", "concerns": [], "reason": "clean"}


def _plan_graph_resolve(sandbox, plan_text, *, project_id="proj"):
    """Runs Phases 1/3/4/5 for real against the sandbox's own taxonomy,
    persists plan + graph the same way `capability plan`/`capability
    resolve` CLI commands do, and returns (plan, graph)."""
    from core.capability.requirements import analyse_requirements
    from core.capability.graph import build_graph, save_graph
    from core.capability.resolver import resolve_project
    from core.projectspec import parse_project_spec

    a = str(sandbox["agentic"])
    spec = parse_project_spec(plan_text)
    plan = analyse_requirements(spec, TAXONOMY, project_id=project_id)
    projstate.write_yaml(a, "capability-plan.yaml", plan)
    graph = build_graph(spec, plan, TAXONOMY, project_id=project_id)
    resolve_project(graph, TAXONOMY, project_id=project_id)
    save_graph(a, graph)
    return plan, graph


# -- 1. SEO static site: plan -> graph -> resolve -> enriched cycle -> complete ---------

SEO_PLAN = """---
project_type: static_site
---
## Functional Requirements

- The hotel website must rank for boutique hotel searches in Kathmandu.

## Acceptance Criteria

- The site ranks for boutique hotel Kathmandu related queries.
"""


def test_fixture_1_seo_project_resolves_and_completes(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    plan, graph = _plan_graph_resolve(sandbox, SEO_PLAN)
    assert "technical_seo" in {r["capability_id"]
                               for r in plan["required_capabilities"]}

    order = proj_order(task, item="improve SEO rankings for the site",
                       spec="add sitemap and robots")
    caller = FakeCaller({"conductor": order, "coder": worker_out(),
                         "qa": verifier_out("pass"), "security": _SEC_PASS})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock(),
                       run_id="c1")
    assert result["status"] == "success"

    wo = [c for c in caller.calls if c["role"] == "coder"][0]["input"][
        "work_order"]
    assert "technical_seo" in wo["required_capabilities"]

    criteria = projstate.read_yaml(a, "acceptance-criteria.yaml")
    criteria["requirements_map"] = [
        {"requirement": "value is 2", "tasks": [task["id"]]}]
    projstate.write_yaml(a, "acceptance-criteria.yaml", criteria)
    audit = final_audit(sandbox["cfg"], caller=FakeCaller(
        {"qa": verifier_out("pass")}), clock=Clock())
    assert audit["status"] == "complete"
    assert audit["audit"]["completion_contract"]["complete"] is True

    graph_after = load_graph(a)
    assert graph_after.state_of("cap:technical_seo") in (
        "satisfied", "available")


# -- 2. Supabase: capability plan narrows exactly the authorised exception -------------

SUPABASE_PLAN = """---
project_type: web_application
---
## Functional Requirements

- Use Supabase for authentication and the database.
"""


def test_fixture_2_supabase_migration_path_authorised_by_plan(sandbox):
    project_cfg(sandbox)
    task = simple_task(expected_paths=["supabase/migrations/**"])
    seed_project(sandbox, [task])
    plan, graph = _plan_graph_resolve(sandbox, SUPABASE_PLAN)
    assert "supabase" in {r["capability_id"] for r in
                          plan["required_capabilities"]
                          + plan["optional_capabilities"]}

    order = proj_order(task, allowed_paths=[
        "supabase/migrations/0001_init.sql"])
    caller = FakeCaller({"conductor": order, "coder": worker_out(edits=[
        {"path": "supabase/migrations/0001_init.sql", "action": "write",
         "content": "create table t (id int);\n"}]),
        "qa": verifier_out("pass"), "security": _SEC_PASS})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock(),
                       run_id="c1")
    assert result["status"] == "success"   # would hard-fail without a plan


def test_fixture_2b_same_path_blocked_without_a_capability_plan(sandbox):
    project_cfg(sandbox)
    task = simple_task(expected_paths=["supabase/migrations/**"])
    seed_project(sandbox, [task])   # deliberately: no capability plan
    order = proj_order(task, allowed_paths=[
        "supabase/migrations/0001_init.sql"])
    caller = FakeCaller({"conductor": order, "coder": worker_out(),
                         "qa": verifier_out("pass"), "security": _SEC_PASS})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock(),
                       run_id="c1")
    assert result["status"] == "failure"
    assert "protected path" in result["detail"]


# -- 3. Payments: a mandatory high-risk capability never auto-resolves -----------------

PAYMENTS_PLAN = """---
project_type: web_application
---
## Functional Requirements

- Users can complete checkout with a credit card.
"""


def test_fixture_3_mandatory_high_risk_capability_escalates_not_completes(
        sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    plan, graph = _plan_graph_resolve(sandbox, PAYMENTS_PLAN)
    payments = next(r for r in plan["required_capabilities"]
                    if r["capability_id"] == "payments")
    assert payments["mandatory"] is True
    graph_after = load_graph(a)
    assert graph_after.state_of("cap:payments") == "blocked"   # never faked

    criteria = projstate.read_yaml(a, "acceptance-criteria.yaml")
    criteria["requirements_map"] = [
        {"requirement": payments["source_requirement"], "tasks": []}]
    projstate.write_yaml(a, "acceptance-criteria.yaml", criteria)
    order = proj_order(task)
    caller = FakeCaller({"conductor": order, "coder": worker_out(),
                         "qa": verifier_out("pass"), "security": _SEC_PASS})
    assert run_cycle(sandbox["cfg"], caller=caller, clock=Clock(),
                     run_id="c1")["status"] == "success"
    audit = final_audit(sandbox["cfg"], caller=FakeCaller(
        {"qa": verifier_out("pass")}), clock=Clock())
    # the backlog is complete, but the Completion Contract still honestly
    # reports the payments requirement as unverified -- never claims
    # completion without evidence
    assert audit["status"] == "audit_failed"
    assert "completion_contract_verified" in audit["failed_checks"]


# -- 4. Hierarchical orchestration flows into the enriched work order ------------------

def test_fixture_4_frontier_reserve_exhaustion_flows_into_selected_model(
        sandbox):
    from core.capacity import CapacityLedger
    from core.modelcap import ModelCapabilityRegistry, model_record
    from core.capability.workorder import enrich_work_order

    project_cfg(sandbox)
    cfg = sandbox["cfg"]
    # an admin has configured ordinary worker tasks to require frontier-
    # class models (unusual, but the exact scenario the reserve exists
    # to protect against): coder never reserves frontier capacity, so
    # once the reserve is exhausted it must demote, never starve a
    # reserving role (architect/final_auditor) of frontier capacity
    cfg["orchestration"] = {"workers": {"default_class": "frontier"},
                            "orchestrator": {"reserve_capacity_percent": 25}}
    memory_dir = str(sandbox["agentic"] / "memory")
    ledger = CapacityLedger(cfg, memory_dir)
    registry = ModelCapabilityRegistry([
        model_record(backend="frontier-cli", provider="frontier-cli",
                    model_id="big", available=True,
                    reasoning_class="frontier"),
        model_record(backend="mid-cli", provider="mid-cli", model_id="mid",
                    available=True, reasoning_class="high")])
    # saturate the reserve with non-reserving ("coder") calls so the next
    # coder-role selection is forced to demote off frontier
    for i in range(20):
        ledger.record_call("frontier-cli", "coder", True,
                           usage={"input_tokens": 10, "output_tokens": 10,
                                  "cached_input_tokens": 0})

    order = {"item": "implement the feature", "spec": "", "objective": "",
            "risk": "low"}
    enriched = enrich_work_order(order, {"id": "t1"}, role="coder",
                                 model_registry=registry, ledger=ledger,
                                 cfg=cfg)
    assert enriched["selected_backend"] == "mid-cli"   # demoted, not frontier


# -- 5. Skill acquisition flows through the graph into work-order selection ------------

def test_fixture_5_auto_approved_skill_flows_into_selected_skills(sandbox):
    from core.capability.requirements import analyse_requirements
    from core.capability.graph import build_graph, save_graph
    from core.capability.resolver import resolve_project
    from core.capability.workorder import enrich_work_order
    from core.projectspec import parse_project_spec

    a = str(sandbox["agentic"])
    spec = parse_project_spec(SEO_PLAN)
    plan = analyse_requirements(spec, TAXONOMY, project_id="proj")
    graph = build_graph(spec, plan, TAXONOMY, project_id="proj")

    def registry_search(cap_def):
        if cap_def["id"] != "technical_seo":
            return []
        return [{"type": "skill", "source": "market",
                 "name": "seo-optimization", "status": "available",
                 "risk": "low", "trust": 1.0}]

    resolve_project(graph, TAXONOMY, project_id="proj",
                    registry_search=registry_search)
    assert graph.state_of("cap:technical_seo") in ("available", "satisfied")
    save_graph(a, graph)

    order = {"item": "improve SEO rankings", "spec": "", "objective": "",
            "risk": "low"}
    enriched = enrich_work_order(order, {"id": "t1"}, graph=graph,
                                 taxonomy=TAXONOMY)
    assert "seo-optimization" in enriched["selected_skills"]


# -- 6. Full lifecycle, verified back through the Phase 12 dashboard -------------------

def test_fixture_6_full_lifecycle_matches_dashboard_snapshot(tmp_path,
                                                              monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import copy

    monkeypatch.setenv("AGENTIC_HOME", str(tmp_path / "home"))
    from core import setupwiz
    monkeypatch.setattr(setupwiz, "detect_backends",
                        lambda cfg, **kw: ({}, {}))
    import core.config as config_mod
    agentic = tmp_path / "agentic"
    import shutil
    for sub in ("prompts", "schemas", "guardrails", "capabilities"):
        shutil.copytree(AGENTIC_SRC / sub, agentic / sub)
    for sub in ("memory", "queue", "runs", "goals", "worktrees"):
        (agentic / sub).mkdir(parents=True)
    monkeypatch.setattr(config_mod, "AGENTIC_DIR", agentic)

    cfg = copy.deepcopy({
        "version": 1, "project": {"name": "test", "repository_root": ".."},
        "execution": {"mode": "review", "max_tasks_per_run": 1,
                      "max_changed_lines": 400, "max_changed_files": 20,
                      "worktree_enabled": True, "command_timeout_seconds": 60,
                      "goal_timeout_seconds": 10, "safe_commands": []},
        "roles": {}, "providers": {}, "budget": {}, "pricing": {},
        "retry": {"maximum_attempts_per_provider": 2,
                  "backoff_seconds": [0, 0], "allow_fallback": True,
                  "fallback_on_refusal": False},
        "verification": {"commands": [
            {"name": "ok-check",
             "command": "python -c \"import sys; sys.exit(0)\"",
             "mandatory": True}], "fail_fast": True},
        "trust": {"sensitive_auto_allowed": [], "sensitive_skills": [],
                  "track_by_model": False},
        "contract": {"extra_protected_paths": []},
        "integrations": {"github_cli": "off"},
        "backends": {"mock": {"type": "api", "provider": "mock",
                              "model": "mock-model"}},
        "routing": {"mode": "simple", "primary": "mock", "fallbacks": []},
        "repair": {"maximum_attempts_per_task": 3},
        "capacity": {"safety_multiplier": 1.35},
        "interaction": {"mode": "completion_only"},
        "notifications": {"desktop": False}, "limits": {},
        "scheduler": {"cooling": {"after_success_minutes": 30,
                                  "after_failure_minutes": 30,
                                  "minimum_minutes": 5,
                                  "maximum_minutes": 360},
                     "continuation": {"automatic": True},
                     "operating_window": {"enabled": False}},
    })

    from ui.app import create_app
    app = create_app(load_cfg=lambda: cfg, detector=lambda cfg: ({}, {}))
    client = TestClient(app, base_url="http://127.0.0.1")

    root = tmp_path / "apps" / "hotel-app"
    root.mkdir(parents=True)
    (root / "plan.md").write_text(SEO_PLAN, encoding="utf-8")
    r = client.post("/api/v1/portfolio/add",
                    json={"name": "hotel-app", "root": str(root)})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert client.post("/api/v1/portfolio/%s/init" % pid,
                       json={}).status_code == 200

    from core.registry import ProjectRegistry
    registry = ProjectRegistry()
    record = registry.get(pid)
    plan = projectops.analyse_capabilities(registry, record)
    projectops.save_capability_plan(registry, pid, plan)
    graph = projectops.build_capability_graph(registry, record, plan=plan)
    projectops.resolve_capabilities(registry=registry, cfg=cfg,
                                    record=record, graph=graph)
    projectops.save_capability_graph(registry, pid, graph)

    proj_cfg = projectops.project_cfg_for(cfg, registry, record)
    started = project_start(proj_cfg, str(root / "plan.md"), caller=
                            FakeCaller({"architect": {
                                "architecture": "static site",
                                "assumptions": [], "milestones": [
                                    {"id": "m1", "title": "core"}],
                                "backlog": [{
                                    "id": "t1", "milestone": "m1",
                                    "description": "add sitemap.xml",
                                    "dependencies": [], "risk": "low",
                                    "security_relevant": False,
                                    "expected_paths": ["public/**"],
                                    "expected_size": "small",
                                    "acceptance_criteria": ["sitemap present"],
                                    "deterministic_checks": [],
                                    "skill": "app-code"}],
                                "requirements_map": [], "human_decisions": [],
                                "completion_criteria": ["site is live"]}}),
                            clock=Clock())
    assert started["status"] == "started"

    order = {"action": "execute", "item": "improve SEO rankings",
             "skill": "app-code", "spec": "add sitemap and robots",
             "done_when": [{"id": "DW-1", "condition": "ok-check passes",
                            "command": None}],
             "allowed_paths": ["public/**"], "forbidden_paths": [],
             "maximum_changed_lines": 10, "risk": "low",
             "queue_reason": None}
    caller = FakeCaller({"conductor": order, "coder": worker_out(
        edits=[{"path": "public/sitemap.xml", "action": "write",
               "content": "<urlset/>\n"}]),
        "qa": verifier_out("pass"), "security": _SEC_PASS})
    cycle_result = run_cycle(proj_cfg, caller=caller, clock=Clock(),
                             run_id="c1")
    assert cycle_result["status"] == "success"
    final_audit(proj_cfg, caller=FakeCaller({"qa": verifier_out("pass")}),
               clock=Clock())

    snap = client.get("/api/v1/portfolio/%s/capability" % pid).json()
    assert "technical_seo" in snap["plan_summary"]["required_capabilities"]
    assert snap["graph_summary"]["capability_count"] > 0
    assert snap["completion_contract"] is not None
