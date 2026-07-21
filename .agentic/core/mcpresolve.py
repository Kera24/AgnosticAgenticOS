"""MCP Capability Resolution (Phase 7).

For a capability that declares `suggested_mcp_capabilities`, search
already-configured MCP servers first (`core.mcp.MCPGateway.list()` --
unchanged, real enable/review/auth gating). If none is usable, check a
declarative catalog of known-safe LOCAL server templates (admin-
configured; `.agentic/mcp-templates.yaml` + an `org/` override directory,
mirroring the capability taxonomy's override pattern) and auto-configure
one only when every safety criterion holds. When a server needs OAuth,
sensitive external-account access, paid enrolment, or production
mutation, nothing is auto-configured -- instead exactly ONE setup action
is persisted per (project, server) pair (never duplicated, never
re-prompted); a later resolve pass picks it up automatically once
`core.mcp`'s own `authenticate()`/health-check machinery reports success.

Environment/access policy mirrors `core.supabasex.environment_policy()`
exactly: local is always safe to auto-configure, staging/production
never are.
"""
import datetime as _dt
import os
import uuid

import yaml

from . import errors, projstate

ENVIRONMENT_POLICY = {
    "local": {"auto_configure": True},
    "development": {"auto_configure": True},
    "staging": {"auto_configure": False},
    "production": {"auto_configure": False},
}

DEFAULT_MCP_POLICY = {
    "prefer_official": True,
    "approved_sources": ("builtin", "internal", "local_index"),
    "hosted_default_read_only": True,
}

SETUP_ACTION_KINDS = ("oauth", "sensitive_account", "paid_service",
                     "production_mutation")


class MCPResolveError(errors.PolicyError):
    """An MCP-resolution invariant was violated."""


def policy_from_cfg(cfg):
    merged = dict(DEFAULT_MCP_POLICY)
    merged.update((cfg.get("mcp") or {}).get("resolution") or {})
    return merged


# -- known-safe local server templates (declarative, admin-configured) -------------

def _default_paths(agentic_dir):
    base = os.path.join(str(agentic_dir), "mcp-templates.yaml")
    org_dir = os.path.join(str(agentic_dir), "mcp-templates", "org")
    org_files = []
    if os.path.isdir(org_dir):
        org_files = sorted(os.path.join(org_dir, f)
                           for f in os.listdir(org_dir)
                           if f.endswith((".yaml", ".yml")))
    return base, org_files


def load_local_templates(agentic_dir=None):
    """Returns {capability_hint_name: template_dict}. A missing catalog
    file is not an error -- it just means no local template is known
    yet (auto-configuration simply won't find anything)."""
    if agentic_dir is None:
        from .config import AGENTIC_DIR
        agentic_dir = AGENTIC_DIR
    base, org_files = _default_paths(agentic_dir)
    templates = {}
    for path in [base] + org_files:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        for name, template in (data.get("templates") or {}).items():
            templates[name] = template
    return templates


# -- safety evaluation (deterministic, never a model) --------------------------------

def is_safe_to_auto_configure(template, *, policy=None):
    """Returns (safe: bool, blocking_kind: str-or-None, reasons: [str]).
    `blocking_kind` is one of SETUP_ACTION_KINDS when a one-time human
    action is what's actually needed (never a hard failure)."""
    policy = policy or DEFAULT_MCP_POLICY
    environment = template.get("environment", "local")
    env_policy = ENVIRONMENT_POLICY.get(environment,
                                        ENVIRONMENT_POLICY["production"])
    if not env_policy["auto_configure"]:
        return False, "production_mutation", [
            "environment %r never auto-configures (staging/production "
            "require explicit approval)" % environment]
    if template.get("production_mutation"):
        return False, "production_mutation", ["template declares "
                                               "production mutation"]
    auth_type = template.get("authentication_type", "none")
    if auth_type == "oauth":
        return False, "oauth", ["server requires an OAuth browser login"]
    if template.get("sensitive_account"):
        return False, "sensitive_account", ["server requires sensitive "
                                            "external account access"]
    if template.get("paid_service"):
        return False, "paid_service", ["server requires paid service "
                                       "enrolment"]
    checks = {
        "command_approved": template.get("source") in
        policy["approved_sources"],
        "project_scoped": template.get("scope", "project") == "project",
        "filesystem_scope_bounded": bool(template.get("filesystem_scope")) or
        not template.get("filesystem_access", False),
        "tools_allowlisted": bool(template.get("allowed_tools")),
        "output_limit_set": bool(template.get("maximum_output_tokens")),
        "no_production_mutation": not template.get("production_mutation"),
    }
    failing = [name for name, ok in checks.items() if not ok]
    if failing:
        return False, None, ["failing: %s" % ", ".join(failing)]
    return True, None, ["every auto-configuration criterion satisfied"]


# -- setup-action queue (the "autonomy inbox" backing store) ------------------------

def create_setup_action(runtime_dir, *, kind, capability_id, server_name,
                        reason):
    """Persist exactly one setup action per (server_name, kind) -- never
    duplicated, never re-prompted on a later resolve pass."""
    if kind not in SETUP_ACTION_KINDS:
        raise MCPResolveError("unknown setup action kind %r" % kind)
    data = projstate.read_yaml(runtime_dir, "setup-actions.yaml",
                               {"actions": []})
    for action in data["actions"]:
        if action["server_name"] == server_name and action["kind"] == kind \
                and action["status"] == "pending":
            return action   # already queued -- do not duplicate
    entry = {
        "id": uuid.uuid4().hex[:12], "kind": kind,
        "capability_id": capability_id, "server_name": server_name,
        "reason": reason, "status": "pending",
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    data["actions"].append(entry)
    projstate.write_yaml(runtime_dir, "setup-actions.yaml", data)
    return entry


def list_setup_actions(runtime_dir, *, status=None):
    data = projstate.read_yaml(runtime_dir, "setup-actions.yaml",
                               {"actions": []})
    return [a for a in data["actions"]
           if status is None or a["status"] == status]


def resolve_setup_action(runtime_dir, action_id, *, status="resolved"):
    data = projstate.read_yaml(runtime_dir, "setup-actions.yaml",
                               {"actions": []})
    for action in data["actions"]:
        if action["id"] == action_id:
            action["status"] = status
            action["resolved_at"] = _dt.datetime.now().isoformat(
                timespec="seconds")
            projstate.write_yaml(runtime_dir, "setup-actions.yaml", data)
            return action
    raise MCPResolveError("unknown setup action %r" % action_id)


# -- resolution: the Phase 5 registry_search-style entry point ----------------------

def resolve_mcp_for_capability(gateway, cap_def, *, project_id, runtime_dir,
                               policy=None, templates=None):
    """Search already-configured servers, then known-safe local
    templates. Returns ResolutionCandidate-shaped dicts (type="mcp_tool")
    -- callers can pass this straight through as (part of) Phase 5's
    `registry_search` hook, exactly like Phase 6's skill acquisition."""
    suggested = cap_def.get("suggested_mcp_capabilities") or []
    if not suggested:
        return []
    policy = policy or DEFAULT_MCP_POLICY
    out = []

    for record in gateway.list(project_id=project_id):
        if not ({record.get("id"), record.get("name")} & set(suggested)):
            continue
        auth_ok = record.get("authentication_status") == "ok" or \
            record.get("authentication_type", "none") == "none"
        if record.get("enabled") and record.get("reviewed") and auth_ok:
            out.append(_result(cap_def["id"], record, "available",
                               "low" if record.get("read_only") else
                               "medium", None))
        elif not auth_ok:
            create_setup_action(
                runtime_dir, kind="oauth", capability_id=cap_def["id"],
                server_name=record["id"],
                reason="authenticate the %r MCP server (one-time "
                      "browser login)" % record["id"])
            out.append(_result(
                cap_def["id"], record, "unavailable", "medium",
                "awaiting authentication -- one setup action created, "
                "will resume automatically once authenticated"))
        else:
            out.append(_result(
                cap_def["id"], record, "unavailable", "medium",
                "server exists but is not enabled/reviewed yet"))

    if any(c["status"] == "available" for c in out):
        return out

    templates = templates if templates is not None else \
        load_local_templates()
    for name in suggested:
        template = templates.get(name)
        if not template:
            continue
        safe, blocking_kind, reasons = is_safe_to_auto_configure(
            template, policy=policy)
        if safe:
            record = _configure(gateway, name, template, project_id)
            out.append(_result(cap_def["id"], record, "available",
                               "low" if record.get("read_only") else
                               "medium", None))
        else:
            if blocking_kind:
                create_setup_action(
                    runtime_dir, kind=blocking_kind,
                    capability_id=cap_def["id"], server_name=name,
                    reason="%s (%s)" % (reasons[0], blocking_kind))
            out.append({
                "capability_id": cap_def["id"], "type": "mcp_tool",
                "source": template.get("source", "local_template"),
                "name": name, "revision": None,
                "risk": "high" if blocking_kind else "medium",
                "status": "unavailable", "rejection_reason": reasons[0],
                "trust": 0.3, "quality_score": 0.5,
                "maintenance_score": 0.5})
    return out


def _configure(gateway, name, template, project_id):
    record = gateway.add(
        name, transport=template.get("transport", "stdio"),
        command=template.get("command"), url=template.get("url"),
        scope="project", project_id=project_id,
        environment=template.get("environment", "local"),
        read_only=template.get("read_only", True),
        allowed_tools=template.get("allowed_tools"),
        denied_tools=template.get("denied_tools"),
        authentication_type=template.get("authentication_type", "none"),
        maximum_output_tokens=template.get("maximum_output_tokens", 4000),
        timeout=template.get("timeout", 30))
    gateway.mark_reviewed(record["id"])
    gateway.enable(record["id"])
    return gateway.get(record["id"])


def _result(capability_id, record, status, risk, reason):
    return {
        "capability_id": capability_id, "type": "mcp_tool",
        "source": record.get("id", "unknown"),
        "name": record.get("name", record.get("id")),
        "revision": None, "risk": risk, "status": status,
        "rejection_reason": reason,
        "trust": 1.0 if status == "available" else 0.3,
        "quality_score": 0.6, "maintenance_score": 0.6,
    }
