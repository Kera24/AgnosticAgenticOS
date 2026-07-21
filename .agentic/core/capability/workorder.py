"""Capability-Aware Planning and Execution (Phase 10): deterministically
enriches a conductor-produced WorkOrder with capability-aware fields --
required_capabilities, selected_skills/mcp_tools/plugin_components,
selected_agent_role/backend/model, evidence_requirements, and
protected_actions -- computed AFTER the conductor call from already-
resolved platform state (the CapabilityGraph, the Canonical Role
Registry, Hierarchical Orchestration, and a persisted Model Capability
Registry snapshot). The conductor model itself never needs to know
anything about capabilities, skills, MCP tools, or model classes: this
module fills those fields in from code, never from a second model call.

`enrich_work_order()` never mutates its `order` argument, never removes
or overrides a field the conductor already set, and never raises for
missing optional inputs -- a project with no CapabilityPlan/
CapabilityGraph yet (or no persisted model registry snapshot) gets back
an equivalent copy of `order` with the new fields left at their honest
empty/None defaults, so today's WorkOrder shape and behaviour are
unchanged for any project that hasn't opted into capability planning.
"""

_SATISFIED_BY_EDGE_TYPES = {
    "capability_satisfied_by_skill": ("selected_skills", "skill"),
    "capability_satisfied_by_mcp": ("selected_mcp_tools", "mcp_tool"),
    "capability_satisfied_by_plugin": ("selected_plugin_components",
                                       "plugin_component"),
}


def _required_capabilities(order, graph, taxonomy):
    """Capabilities in THIS project's plan (i.e. already a node in the
    graph) whose taxonomy triggers match this specific work order's own
    text -- never any taxonomy capability at large, only ones the
    Requirements Intelligence Engine already selected for the project."""
    if graph is None or taxonomy is None:
        return []
    text = " ".join(str(order.get(f) or "") for f in
                    ("item", "spec", "objective"))
    matched = {c["id"] for c in taxonomy.matching_triggers(text)}
    plan_capabilities = {n["attributes"]["capability_id"]
                         for n in graph.nodes_of_type("capability").values()}
    return sorted(matched & plan_capabilities)


def _selections_for(graph, capability_ids):
    """{"selected_skills": [...], "selected_mcp_tools": [...],
    "selected_plugin_components": [...]} from the graph's already-
    resolved `capability_satisfied_by_*` edges -- never a second
    resolution, only a read of what Phase 5/6/7 already chose."""
    out = {"selected_skills": [], "selected_mcp_tools": [],
          "selected_plugin_components": []}
    for cap_id in capability_ids:
        node_id = "cap:%s" % cap_id
        if graph.get_node(node_id) is None:
            continue
        for edge_type, (field, _node_type) in _SATISFIED_BY_EDGE_TYPES.items():
            for edge in graph.edges_from(node_id, edge_type):
                target = graph.get_node(edge["to"])
                name = (target or {}).get("label") or edge["to"]
                if name not in out[field]:
                    out[field].append(name)
    return out


def _evidence_requirements(taxonomy, capability_ids):
    seen, out = set(), []
    for cap_id in capability_ids:
        cap_def = taxonomy.get(cap_id) or {}
        for text in cap_def.get("evidence_requirements") or []:
            if text not in seen:
                seen.add(text)
                out.append(text)
    return out


def _protected_actions(capability_plan):
    if not capability_plan:
        return []
    return list(capability_plan.get("protected_actions") or [])


def enrich_work_order(order, task, *, graph=None, taxonomy=None,
                      capability_plan=None, role=None, model_registry=None,
                      ledger=None, cfg=None):
    """Returns a NEW dict: `order` plus the additive Phase 10 fields.
    Every field degrades to an honest empty/None default when its
    corresponding platform data isn't available."""
    enriched = dict(order)
    if not enriched.get("task_id"):
        enriched["task_id"] = (task or {}).get("id")

    required_ids = _required_capabilities(order, graph, taxonomy)
    enriched.setdefault("required_capabilities", required_ids)
    enriched.setdefault("source_requirements", [])

    selections = _selections_for(graph, required_ids) if graph is not None \
        else {"selected_skills": [], "selected_mcp_tools": [],
             "selected_plugin_components": []}
    for field, values in selections.items():
        enriched.setdefault(field, values)

    enriched.setdefault("selected_agent_role", role)

    enriched.setdefault("selected_backend", None)
    enriched.setdefault("selected_model", None)
    if role and model_registry is not None and cfg is not None:
        from ..hierarchy import select_for_role
        decision = select_for_role(role, cfg, model_registry, ledger,
                                   task_risk=order.get("risk", "medium"))
        if decision["ok"]:
            enriched["selected_backend"] = decision["backend"]
            enriched["selected_model"] = decision["model_id"]

    enriched.setdefault(
        "evidence_requirements",
        _evidence_requirements(taxonomy, required_ids)
        if taxonomy is not None else [])
    enriched.setdefault("protected_actions",
                        _protected_actions(capability_plan))
    return enriched


def record_capability_evidence(graph, required_capability_ids, *, task_id,
                               gate_ok, source="deterministic_check"):
    """Best-effort: record that a task touching these capabilities passed
    its deterministic checks, and mark satisfied capabilities as such.
    Never raises for an unknown/already-satisfied capability -- callers
    (project.py) wrap this defensively regardless, since evidence
    recording must never be able to break a successful cycle."""
    if graph is None or not gate_ok:
        return []
    recorded = []
    for cap_id in required_capability_ids:
        node_id = "cap:%s" % cap_id
        if graph.get_node(node_id) is None:
            continue
        graph.record_evidence(
            node_id, "deterministic checks passed for task %s" % task_id,
            source=source)
        try:
            graph.mark_satisfied(node_id)
            recorded.append(cap_id)
        except Exception:   # noqa: BLE001 — e.g. no matching check edge yet
            pass
    return recorded
