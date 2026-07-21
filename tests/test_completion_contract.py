"""Phase 11 -- Completion Contract and Evidence Matrix: traces each
requirement in the architect's requirements_map to real, already-
verified evidence (done+pass backlog tasks, and/or a satisfied/waived
capability), and wiring into `core.project.final_audit`."""
from conftest import (AGENTIC_SRC, Clock, FakeCaller, project_cfg,
                      proj_order, seed_project, simple_task, verifier_out,
                      worker_out)
from core import projstate
from core.capability import load_taxonomy
from core.capability.graph import build_graph, save_graph
from core.capability.requirements import analyse_requirements
from core.completion import build_completion_contract
from core.project import final_audit, run_cycle
from core.projectspec import parse_project_spec

TAXONOMY = load_taxonomy(agentic_dir=AGENTIC_SRC, strict=True)


def _task(tid, status="pending", last_result=None):
    return {"id": tid, "status": status, "last_result": last_result}


# -- build_completion_contract ---------------------------------------------------------

def test_empty_requirements_map_is_trivially_complete():
    contract = build_completion_contract([], [])
    assert contract == {"requirements": [], "unverified": [],
                        "verified_count": 0, "total_count": 0,
                        "complete": True}


def test_requirement_verified_when_all_linked_tasks_done_and_pass():
    req_map = [{"requirement": "value is 2", "tasks": ["t1"]}]
    backlog = [_task("t1", status="done", last_result="pass")]
    contract = build_completion_contract(req_map, backlog)
    assert contract["complete"] is True
    assert contract["verified_count"] == 1
    assert contract["requirements"][0]["tasks"][0]["verified"] is True


def test_requirement_unverified_when_a_linked_task_is_not_done():
    req_map = [{"requirement": "value is 2", "tasks": ["t1"]}]
    backlog = [_task("t1", status="pending")]
    contract = build_completion_contract(req_map, backlog)
    assert contract["complete"] is False
    assert "value is 2" in contract["unverified"]


def test_requirement_unverified_when_a_task_failed():
    req_map = [{"requirement": "value is 2", "tasks": ["t1", "t2"]}]
    backlog = [_task("t1", status="done", last_result="pass"),
              _task("t2", status="done", last_result="fail")]
    contract = build_completion_contract(req_map, backlog)
    assert contract["complete"] is False


def test_requirement_with_no_tasks_and_no_graph_is_unverified():
    req_map = [{"requirement": "undocumented requirement", "tasks": []}]
    contract = build_completion_contract(req_map, [])
    assert contract["complete"] is False
    assert contract["requirements"][0]["tasks"] == []
    assert contract["requirements"][0]["capabilities"] == []


def test_unknown_task_id_reported_honestly_not_silently_dropped():
    req_map = [{"requirement": "ghost", "tasks": ["does-not-exist"]}]
    contract = build_completion_contract(req_map, [])
    ev = contract["requirements"][0]["tasks"][0]
    assert ev["status"] == "unknown" and ev["verified"] is False


def test_capability_graph_evidence_can_independently_verify_a_requirement():
    md = """---
project_type: static_site
---
## Functional Requirements

- The hotel website must rank for boutique hotel searches.
"""
    spec = parse_project_spec(md)
    plan = analyse_requirements(spec, TAXONOMY, project_id="proj")
    graph = build_graph(spec, plan, TAXONOMY, project_id="proj")
    seo_record = next(r for r in plan["required_capabilities"]
                      if r["capability_id"] == "technical_seo")
    req_text = seo_record["source_requirement"]
    # the requirement text resolves to more than one capability (e.g.
    # frontend_ui too) -- every one of them must be satisfied/waived for
    # the requirement to count as verified, so satisfy them all
    for cap_id in {c["capability_id"] for c in
                  build_completion_contract(
                      [{"requirement": req_text, "tasks": []}], [],
                      graph=graph)["requirements"][0]["capabilities"]}:
        graph.set_state("cap:%s" % cap_id, "available")
        graph.record_evidence("cap:%s" % cap_id, "checks passed",
                              source="deterministic_check")
        graph.mark_satisfied("cap:%s" % cap_id)
    req_map = [{"requirement": req_text, "tasks": []}]
    contract = build_completion_contract(req_map, [], graph=graph)
    assert contract["complete"] is True
    caps = {c["capability_id"]: c["state"]
           for c in contract["requirements"][0]["capabilities"]}
    assert caps["technical_seo"] == "satisfied"


# -- wiring into final_audit -----------------------------------------------------------

def test_final_audit_persists_completion_contract_when_map_empty(sandbox):
    project_cfg(sandbox)
    seed_project(sandbox, [simple_task(status="done", last_result="pass")])
    result = final_audit(sandbox["cfg"],
                         caller=FakeCaller({"qa": verifier_out("pass")}),
                         clock=Clock())
    assert result["status"] == "complete"
    audit = projstate.read_yaml(str(sandbox["agentic"]), "final-audit.yaml")
    assert audit["checks"]["completion_contract_verified"] is True
    assert audit["completion_contract"]["complete"] is True


def test_final_audit_blocks_on_unverified_requirement(sandbox):
    project_cfg(sandbox)
    task = simple_task(status="done", last_result="pass")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    criteria = projstate.read_yaml(a, "acceptance-criteria.yaml")
    criteria["requirements_map"] = [
        {"requirement": "a requirement nothing implements", "tasks": []}]
    projstate.write_yaml(a, "acceptance-criteria.yaml", criteria)
    result = final_audit(sandbox["cfg"],
                         caller=FakeCaller({"qa": verifier_out("pass")}),
                         clock=Clock())
    assert result["status"] == "audit_failed"
    assert "completion_contract_verified" in result["failed_checks"]


def test_full_project_build_still_completes_with_evidenced_requirements(
        sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    criteria = projstate.read_yaml(a, "acceptance-criteria.yaml")
    criteria["requirements_map"] = [
        {"requirement": "value is 2", "tasks": [task["id"]]}]
    projstate.write_yaml(a, "acceptance-criteria.yaml", criteria)
    caller = FakeCaller({"conductor": proj_order(task), "coder": worker_out(),
                         "qa": verifier_out("pass"),
                         "security": {"verdict": "pass", "concerns": [],
                                      "reason": "clean"}})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock(),
                       run_id="c1")
    assert result["status"] == "success"
    audit = final_audit(sandbox["cfg"],
                        caller=FakeCaller({"qa": verifier_out("pass")}),
                        clock=Clock())
    assert audit["status"] == "complete"
