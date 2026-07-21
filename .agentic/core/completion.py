"""Completion Contract and Evidence Matrix (Phase 11): traces every
requirement in the architect's `requirements_map` to the concrete,
already-verified evidence that satisfies it -- never a bare claim.

Two evidence sources, either sufficient on its own:
  1. backlog tasks: a task only ever reaches status "done" with
     last_result "pass" after its deterministic gate AND an independent
     QA reviewer both passed it (`project.py`'s existing cycle rules) --
     that is already real, verified evidence, for every project, with or
     without a Capability Plan.
  2. the Capability Graph (Phase 4/10), for requirements that resolve to
     a capability: `satisfied`/`waived` state, which itself can only be
     reached through `mark_satisfied()`'s own verified-evidence
     requirement (never a model's bare claim either).

This module is a pure, deterministic READ over already-persisted state:
it verifies nothing itself and calls no model -- it only assembles what
the platform already proved during ordinary cycle execution into a
traceable per-requirement report. A project with an empty
`requirements_map` gets an empty, trivially-complete contract: this
extends `final_audit`, it never narrows what already passed before.
"""


def _task_evidence(task_id, tasks_by_id):
    task = tasks_by_id.get(task_id)
    if task is None:
        return {"task_id": task_id, "status": "unknown", "last_result": None,
                "verified": False}
    verified = task.get("status") == "done" and task.get(
        "last_result") == "pass"
    return {"task_id": task_id, "status": task.get("status"),
            "last_result": task.get("last_result"), "verified": verified}


def _capability_evidence_for(requirement_text, graph):
    """Capabilities this requirement resolves to, per the Capability
    Graph's own `requirement_requires_capability` edges (Phase 4) --
    matched by the requirement's own source text, never guessed."""
    if graph is None:
        return []
    out = []
    for req_id, req_node in graph.nodes_of_type("requirement").items():
        attrs = req_node["attributes"]
        if attrs.get("source_text") != requirement_text and \
                req_node.get("label") != (requirement_text or "")[:120]:
            continue
        for edge in graph.edges_from(req_id,
                                     "requirement_requires_capability"):
            cap_node = graph.get_node(edge["to"])
            if cap_node is not None:
                out.append({
                    "capability_id": cap_node["attributes"]["capability_id"],
                    "state": cap_node["attributes"]["state"]})
    return out


def build_completion_contract(requirements_map, backlog, *, graph=None):
    """Returns the Evidence Matrix: {"requirements": [...], "unverified":
    [...], "verified_count", "total_count", "complete"}."""
    tasks_by_id = {t["id"]: t for t in backlog}
    requirements = []
    for entry in requirements_map or []:
        task_ids = list(entry.get("tasks") or [])
        task_evidence = [_task_evidence(tid, tasks_by_id) for tid in task_ids]
        capability_evidence = _capability_evidence_for(
            entry.get("requirement"), graph)

        task_verified = bool(task_ids) and all(
            e["verified"] for e in task_evidence)
        capability_verified = bool(capability_evidence) and all(
            c["state"] in ("satisfied", "waived")
            for c in capability_evidence)
        verified = task_verified or capability_verified

        requirements.append({
            "requirement": entry.get("requirement"),
            "tasks": task_evidence,
            "capabilities": capability_evidence,
            "verified": verified,
        })
    unverified = [r["requirement"] for r in requirements if not r["verified"]]
    return {"requirements": requirements, "unverified": unverified,
           "verified_count": len(requirements) - len(unverified),
           "total_count": len(requirements),
           "complete": not unverified}
