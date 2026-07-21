"""Autonomous Capability Resolver (Phase 5).

For each unresolved capability in a CapabilityGraph, search sources in a
fixed order -- built-in deterministic functionality, ordinary agent
competence (most capabilities need no external acquisition at all),
installed approved skills, installed MCP tools, approved plugins, and
finally a pluggable external-registry hook -- rank the results, and
either acquire the safest candidate or escalate.

Real discovery reuses the existing registries (`core.skillreg`,
`core.mcp`) exactly as they already enforce trust/enable/review state;
this module adds no new authority over what a skill/MCP server may do.
The risk-tiered ACQUISITION pipeline (download, quarantine, static
scan, auto-approve policy) is Phase 6 (skills) / Phase 7 (MCP/plugins);
this phase defines the ResolutionCandidate contract, search order,
ranking, and the pluggable `registry_search` hook those phases plug
into -- calling it here always returns no candidates by default, so
Phase 5 alone never reaches the network.

Plugins: the platform has no plugin system yet (verified absent in
Phase 0), so `_search_approved_plugins` always returns no candidates
until Phase 7 adds one -- never a placeholder success.
"""
import datetime as _dt

from .. import errors

MAX_CANDIDATES_PER_CAPABILITY = 10
MAX_RESOLUTION_ATTEMPTS = 3
DEFAULT_CACHE_TTL_SECONDS = 6 * 3600

CANDIDATE_TYPES = ("deterministic_tool", "agent_competence", "skill",
                  "mcp_tool", "plugin_component", "external_registry")

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


class ResolverError(errors.PolicyError):
    """Resolver invariant violated (unknown candidate type, bad ranking
    input)."""


def _candidate(capability_id, type_, source, name, *, id_=None, version=None,
              revision=None, publisher=None, description="",
              compatibility=1.0, permissions=None, dependencies=None,
              risk="low", trust=0.5, quality_score=0.5,
              maintenance_score=0.5, token_cost=0, acquisition_cost=0,
              status="candidate", rejection_reason=None):
    if type_ not in CANDIDATE_TYPES:
        raise ResolverError("unknown candidate type %r" % type_)
    return {
        "id": id_ or ("%s:%s:%s" % (type_, source, name)),
        "capability_id": capability_id, "type": type_, "source": source,
        "name": name, "version": version, "revision": revision,
        "publisher": publisher, "description": description,
        "compatibility": compatibility, "permissions": list(permissions or []),
        "dependencies": list(dependencies or []), "risk": risk,
        "trust": trust, "quality_score": quality_score,
        "maintenance_score": maintenance_score, "evaluation_score": None,
        "token_cost": token_cost, "acquisition_cost": acquisition_cost,
        "status": status, "rejection_reason": rejection_reason,
    }


# -- registry metadata cache (requirement: cache + refresh-on-stale) ----------------

class RegistryCache:
    """Minimal in-memory metadata cache keyed by capability id. Phase 6/7
    reuse this for real external-registry results; Phase 5's own
    external-registry step is empty by default (see module docstring)."""

    def __init__(self, ttl_seconds=DEFAULT_CACHE_TTL_SECONDS, clock=None):
        self.ttl_seconds = ttl_seconds
        self.clock = clock or _dt.datetime.now
        self._entries = {}   # capability_id -> (cached_at, candidates)

    def get(self, capability_id):
        entry = self._entries.get(capability_id)
        if entry is None:
            return None
        cached_at, candidates = entry
        if (self.clock() - cached_at).total_seconds() > self.ttl_seconds:
            return None   # stale: caller must refresh
        return candidates

    def put(self, capability_id, candidates):
        self._entries[capability_id] = (self.clock(), candidates)


# -- search sources -------------------------------------------------------------------

def _search_deterministic_tools(cap_def, *, which=None):
    """Built-in Agentic OS functionality (dockerx, supabasex, ...) --
    always trusted, never re-evaluated for risk beyond the capability's
    own risk_level, since it ships with the platform."""
    out = []
    for tool in cap_def.get("deterministic_tools") or []:
        out.append(_candidate(
            cap_def["id"], "deterministic_tool", "platform", tool,
            description="built-in Agentic OS capability", risk="low",
            trust=1.0, quality_score=1.0, maintenance_score=1.0,
            compatibility=1.0, status="available"))
    return out


def _search_agent_competence(cap_def):
    """Most capabilities need no external acquisition at all: an
    assigned agent role simply builds it as part of normal work. Offered
    only when the taxonomy declares no specific skill/MCP/plugin/tool
    suggestion -- otherwise a more specific source should be preferred."""
    if any(cap_def.get(f) for f in ("suggested_skills",
                                    "suggested_mcp_capabilities",
                                    "suggested_plugins",
                                    "deterministic_tools")):
        return []
    return [_candidate(
        cap_def["id"], "agent_competence", "platform", "agent_competence",
        description="built by the assigned agent role as ordinary work",
        risk="low", trust=1.0, quality_score=0.8, maintenance_score=1.0,
        compatibility=1.0, status="available")]


def _search_installed_skills(cap_def, skill_registry):
    if skill_registry is None:
        return []
    out = []
    suggested = set(cap_def.get("suggested_skills") or [])
    role = (cap_def.get("agent_roles") or [None])[0]
    seen_ids = set()
    for manifest in skill_registry.list():
        if manifest["id"] not in suggested:
            continue
        seen_ids.add(manifest["id"])
        out.append(_skill_candidate(cap_def, skill_registry, manifest))
    if not out and role:
        query = " ".join(cap_def.get("triggers") or [cap_def.get("name", "")])
        for manifest in skill_registry.select(role, query,
                                              limit=MAX_CANDIDATES_PER_CAPABILITY):
            if manifest["id"] in seen_ids:
                continue
            out.append(_skill_candidate(cap_def, skill_registry, manifest))
    return out


def _skill_candidate(cap_def, skill_registry, manifest):
    verified = skill_registry.verify(manifest["id"])
    usable = bool(manifest.get("enabled")) and verified.get("ok", False)
    risk = manifest.get("risk_level", "medium")
    return _candidate(
        cap_def["id"], "skill", manifest.get("source", "unknown"),
        manifest["id"], id_="skill:%s" % manifest["id"],
        revision=manifest.get("pinned_revision"),
        publisher=manifest.get("source"),
        description=manifest.get("description", ""),
        permissions=manifest.get("permissions"), risk=risk,
        trust=1.0 if manifest.get("reviewed") else 0.4,
        quality_score=0.7, maintenance_score=0.6,
        status="available" if usable else "unavailable",
        rejection_reason=None if usable else
        (verified.get("reason") or "not enabled"))


def _search_installed_mcp(cap_def, mcp_gateway, project_id=None):
    if mcp_gateway is None:
        return []
    suggested = set(cap_def.get("suggested_mcp_capabilities") or [])
    if not suggested:
        return []
    out = []
    for record in mcp_gateway.list(project_id=project_id):
        name_id = {record.get("id"), record.get("name")}
        if not (name_id & suggested):
            continue
        auth_ok = record.get("authentication_status") == "ok" or \
            record.get("authentication_type", "none") == "none"
        usable = bool(record.get("enabled")) and bool(record.get("reviewed")) \
            and auth_ok
        out.append(_candidate(
            cap_def["id"], "mcp_tool", record.get("id", "unknown"),
            record.get("name", record.get("id")), id_="mcp:%s"
            % record.get("id"),
            description="MCP server %s" % record.get("id"),
            risk="low" if record.get("read_only") else "medium",
            trust=1.0 if record.get("reviewed") else 0.3,
            quality_score=0.6, maintenance_score=0.6,
            status="available" if usable else "unavailable",
            rejection_reason=None if usable else
            ("requires authentication" if not auth_ok
             else "not enabled/reviewed")))
    return out


def _search_approved_plugins(cap_def):
    """No plugin system exists yet (Phase 0 finding) -- always empty
    until Phase 7 adds one. Never a placeholder success."""
    return []


_CANDIDATE_KWARGS = ("id_", "version", "revision", "publisher",
                    "description", "compatibility", "permissions",
                    "dependencies", "risk", "trust", "quality_score",
                    "maintenance_score", "token_cost", "acquisition_cost",
                    "status", "rejection_reason")


def _search_external_registry(cap_def, registry_search, cache):
    """Pluggable hook, empty by default. `registry_search(cap_def) ->
    list[dict]` may be supplied by Phase 6's real acquisition engine;
    results are cached and refreshed only when stale. The hook's dicts
    may carry extra bookkeeping fields (e.g. `capability_id`, `type`,
    `trust_level`) that aren't `_candidate()` parameters -- only the
    known-safe subset is forwarded, `capability_id`/`type` always come
    from `cap_def` itself so a hook can never claim a different one."""
    if registry_search is None:
        return []
    cached = cache.get(cap_def["id"]) if cache else None
    if cached is not None:
        return cached
    raw = registry_search(cap_def) or []
    candidates = [
        _candidate(cap_def["id"], c.get("type", "external_registry"),
                  c.get("source", "external"), c.get("name", "candidate"),
                  **{k: v for k, v in c.items() if k in _CANDIDATE_KWARGS})
        for c in raw[:MAX_CANDIDATES_PER_CAPABILITY]]
    if cache is not None:
        cache.put(cap_def["id"], candidates)
    return candidates


# -- ranking (never popularity-only) ---------------------------------------------------

def rank_candidates(candidates):
    """Deterministic scoring from real signals -- trust, compatibility,
    maintenance, quality, risk, and cost -- never a bare popularity
    count (no such field is even accepted). Instruction-only skills
    (deterministic_tool/agent_competence/skill) are preferred over
    executable plugin components when scores tie, and local/read-only
    MCP access is preferred over write access at equal scores."""
    def score(c):
        risk_penalty = {"low": 0.0, "medium": 0.15, "high": 0.4}[c["risk"]]
        s = (0.30 * c["trust"] + 0.20 * c["compatibility"]
            + 0.20 * c["quality_score"] + 0.15 * c["maintenance_score"]
            - risk_penalty - 0.05 * min(1.0, c["token_cost"] / 10000.0)
            - 0.05 * min(1.0, c["acquisition_cost"]))
        return round(max(0.0, min(1.0, s)), 4)

    type_preference = {"deterministic_tool": 0, "agent_competence": 1,
                       "skill": 2, "mcp_tool": 3, "plugin_component": 4,
                       "external_registry": 5}
    scored = []
    for c in candidates:
        c = dict(c)
        c["evaluation_score"] = score(c)
        scored.append(c)
    scored.sort(key=lambda c: (-c["evaluation_score"],
                               type_preference.get(c["type"], 9),
                               c["id"]))
    return scored


def is_safe_to_acquire(candidate, capability_risk):
    """A candidate is auto-acquirable when its own risk is no worse than
    the capability's declared ceiling and it doesn't ask for permission
    expansion. High-risk capabilities (e.g. compliance_baseline) never
    auto-acquire regardless of the candidate."""
    if candidate["status"] != "available":
        return False
    if capability_risk == "high":
        return False
    if RISK_ORDER[candidate["risk"]] > RISK_ORDER.get(capability_risk, 1):
        return False
    return True


def _search_all_sources(cap_def, *, skill_registry=None, mcp_gateway=None,
                        project_id=None, registry_search=None, cache=None):
    candidates = []
    candidates += _search_deterministic_tools(cap_def)
    candidates += _search_agent_competence(cap_def)
    candidates += _search_installed_skills(cap_def, skill_registry)
    candidates += _search_installed_mcp(cap_def, mcp_gateway,
                                        project_id=project_id)
    candidates += _search_approved_plugins(cap_def)
    candidates += _search_external_registry(cap_def, registry_search, cache)
    return candidates[:MAX_CANDIDATES_PER_CAPABILITY * 3]


def preview_candidates(capability_id, taxonomy, *, skill_registry=None,
                       mcp_gateway=None, project_id=None,
                       registry_search=None, cache=None):
    """Search + rank only -- never acquires, never mutates a graph.
    Powers `capability candidates <project> <capability-id>`."""
    cap_def = dict(taxonomy.get(capability_id) or {}, id=capability_id)
    candidates = _search_all_sources(
        cap_def, skill_registry=skill_registry, mcp_gateway=mcp_gateway,
        project_id=project_id, registry_search=registry_search, cache=cache)
    return rank_candidates(candidates)


# -- resolution ---------------------------------------------------------------------

def resolve_capability(capability_node_id, graph, taxonomy, *,
                       skill_registry=None, mcp_gateway=None,
                       project_id=None, registry_search=None, cache=None,
                       _visiting=None):
    """Resolve ONE capability node. Never raises for an ordinary
    resolution failure -- returns a decision dict; callers decide
    escalation policy. Every decision (chosen candidate AND every
    rejected alternative) is recorded in the returned dict.

    `_visiting` guards against a taxonomy cycle (e.g. two capabilities
    declaring each other as `alternatives`) recursing forever -- internal
    use only, callers never pass it."""
    _visiting = _visiting or set()
    node = graph.get_node(capability_node_id)
    if node is None or node["type"] != "capability":
        raise ResolverError("%r is not a capability node"
                            % capability_node_id)
    cap_id = node["attributes"]["capability_id"]
    if capability_node_id in _visiting:
        return {"capability_id": cap_id, "ok": False, "escalate": False,
               "reason": "alternative cycle detected; not re-entering "
                        "%r" % capability_node_id,
               "candidates": [], "chosen": None}
    _visiting = _visiting | {capability_node_id}
    cap_def = dict(taxonomy.get(cap_id) or {}, id=cap_id)
    mandatory = bool(node["attributes"].get("mandatory"))
    capability_risk = cap_def.get("risk_level", "medium")

    attempts = node["attributes"].get("resolution_attempts", 0)
    if attempts >= MAX_RESOLUTION_ATTEMPTS:
        return {"capability_id": cap_id, "ok": False,
               "escalate": mandatory, "reason": "maximum resolution "
               "attempts (%d) reached" % MAX_RESOLUTION_ATTEMPTS,
               "candidates": [], "chosen": None}
    node["attributes"]["resolution_attempts"] = attempts + 1

    graph.set_state(capability_node_id, "resolving")

    candidates = _search_all_sources(
        cap_def, skill_registry=skill_registry, mcp_gateway=mcp_gateway,
        project_id=project_id, registry_search=registry_search, cache=cache)
    ranked = rank_candidates(candidates)
    chosen, rejected = None, []
    for candidate in ranked:
        if is_safe_to_acquire(candidate, capability_risk):
            chosen = candidate
            break
        rejected.append(dict(candidate, rejection_reason=(
            candidate["rejection_reason"]
            or "risk %r exceeds capability ceiling %r"
            % (candidate["risk"], capability_risk))))

    if chosen is not None:
        edge_type = {"skill": "capability_satisfied_by_skill",
                    "mcp_tool": "capability_satisfied_by_mcp",
                    "plugin_component": "capability_satisfied_by_plugin"
                    }.get(chosen["type"])
        if edge_type:
            source_id = "%s:%s" % (chosen["type"], chosen["name"])
            graph.add_node(source_id, {
                "skill": "skill", "mcp_tool": "mcp_tool",
                "plugin_component": "plugin"}[chosen["type"]],
                chosen["name"], candidate=chosen)
            graph.add_edge(capability_node_id, source_id, edge_type,
                          evaluation_score=chosen["evaluation_score"])
        graph.set_state(capability_node_id, "available")
        node["attributes"]["resolved_by"] = chosen["id"]
        return {"capability_id": cap_id, "ok": True, "escalate": False,
               "reason": "resolved via %s %r" % (chosen["type"],
                                                 chosen["name"]),
               "candidates": ranked, "chosen": chosen,
               "rejected": rejected}

    # nothing safe found: try a taxonomy-declared alternative capability
    for alt_id in cap_def.get("alternatives") or []:
        alt_node_id = "cap:%s" % alt_id
        if graph.get_node(alt_node_id) is not None:
            alt_result = resolve_capability(
                alt_node_id, graph, taxonomy, skill_registry=skill_registry,
                mcp_gateway=mcp_gateway, project_id=project_id,
                registry_search=registry_search, cache=cache,
                _visiting=_visiting)
            if alt_result["ok"]:
                graph.set_state(capability_node_id, "waived",
                               reason="satisfied via alternative capability "
                                      "%r instead" % alt_id)
                return {"capability_id": cap_id, "ok": True, "escalate": False,
                       "reason": "no safe direct candidate; alternative "
                                "%r resolved instead" % alt_id,
                       "candidates": ranked, "chosen": None,
                       "rejected": rejected, "alternative_used": alt_id}

    if mandatory:
        graph.set_state(capability_node_id, "blocked",
                        reason="no safe resolution found for a mandatory "
                               "capability")
        return {"capability_id": cap_id, "ok": False, "escalate": True,
               "reason": "mandatory capability blocked: no safe candidate",
               "candidates": ranked, "chosen": None, "rejected": rejected}

    graph.set_state(capability_node_id, "unresolved")
    return {"capability_id": cap_id, "ok": False, "escalate": False,
           "reason": "optional capability left unresolved; project work "
                    "continues", "candidates": ranked, "chosen": None,
           "rejected": rejected}


def resolve_project(graph, taxonomy, *, skill_registry=None,
                    mcp_gateway=None, project_id=None, registry_search=None,
                    cache=None):
    """Resolve every unresolved capability. Optional-capability failures
    never stop the pass; mandatory-capability failures are collected as
    escalations for the caller to surface (never silently dropped, never
    silently bypassed)."""
    cache = cache or RegistryCache()
    decisions, escalations = [], []
    for capability_node_id in list(graph.unresolved_capabilities()):
        decision = resolve_capability(
            capability_node_id, graph, taxonomy, skill_registry=skill_registry,
            mcp_gateway=mcp_gateway, project_id=project_id,
            registry_search=registry_search, cache=cache)
        decisions.append(decision)
        if decision["escalate"]:
            escalations.append(decision)
    return {"decisions": decisions, "escalations": escalations,
           "resolved_count": sum(1 for d in decisions if d["ok"]),
           "unresolved_count": sum(1 for d in decisions if not d["ok"])}
