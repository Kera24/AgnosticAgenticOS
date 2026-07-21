"""Pre-Dispatch Confirmation (Phase 10): a deterministic checklist run
against an already-enriched WorkOrder just before it is handed to a
worker. Advisory in this phase -- `confirm_ready_for_dispatch()` never
raises and never blocks a cycle by itself; `project.py` logs its
warnings so gaps are visible without risking the existing, heavily-
tested live cycle loop on a brand-new gate that has not yet earned a
dedicated hardening/enforcement phase.

Checks (best-effort; any input a caller can't supply is simply skipped,
never guessed at):
  1. every mandatory required_capability is resolved (graph state)
  2. selected skills pass registry verification
  3. selected MCP tools are enabled + reviewed
  4. the selected backend is currently available in the model registry
  5. allowed_paths do not touch a protected path (mirrors project.py's
     own hard check -- this is a second, independent read for visibility,
     never a replacement for it)
"""
from .. import gitops


def confirm_ready_for_dispatch(order, *, graph=None, skill_registry=None,
                               mcp_gateway=None, model_registry=None,
                               protected=None, authorised_exceptions=None):
    """Returns (ok: bool, warnings: [str]). `ok` is True only when there
    are zero warnings; callers decide whether/how to act on either."""
    warnings = []

    if graph is not None:
        for cap_id in order.get("required_capabilities") or []:
            node = graph.get_node("cap:%s" % cap_id)
            if node is None:
                continue
            attrs = node["attributes"]
            if attrs.get("mandatory") and attrs.get("state") not in (
                    "available", "partially_satisfied", "satisfied",
                    "waived"):
                warnings.append(
                    "mandatory capability %r is %r, not yet resolved"
                    % (cap_id, attrs.get("state")))

    if skill_registry is not None:
        for skill_id in order.get("selected_skills") or []:
            try:
                verified = skill_registry.verify(skill_id)
            except Exception as exc:   # noqa: BLE001
                warnings.append("skill %r could not be verified: %s"
                                % (skill_id, exc))
                continue
            if not verified.get("ok"):
                warnings.append("skill %r failed integrity verification: %s"
                                % (skill_id, verified.get("reason")))

    if mcp_gateway is not None:
        from ..mcp import MCPError
        for server_id in order.get("selected_mcp_tools") or []:
            try:
                record = mcp_gateway.get(server_id)
            except MCPError:
                warnings.append("MCP server %r is not configured"
                                % server_id)
                continue
            if not (record.get("enabled") and record.get("reviewed")):
                warnings.append("MCP server %r is not enabled/reviewed"
                                % server_id)

    if model_registry is not None and order.get("selected_backend"):
        available = any(
            r["backend"] == order["selected_backend"] and r["available"]
            for r in model_registry.records)
        if not available:
            warnings.append(
                "selected backend %r is not currently available"
                % order["selected_backend"])

    if protected is not None:
        for pattern in order.get("allowed_paths") or []:
            if gitops.pattern_is_protected(pattern, protected,
                                           authorised_exceptions):
                warnings.append("allowed_paths %r touches a protected path"
                                % pattern)

    return (len(warnings) == 0, warnings)
