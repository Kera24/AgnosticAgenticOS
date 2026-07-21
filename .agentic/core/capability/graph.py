"""Capability Graph (Phase 4): a persistent, per-project, deterministically
rebuildable graph connecting requirements, capabilities, agent roles,
skills, MCP tools, plugins, deterministic tools, tests, acceptance
criteria, and evidence.

Built fresh from the ProjectSpecification (Phase 1) + CapabilityPlan
(Phase 3) + Taxonomy (Phase 2) every time -- nothing here is a second
source of truth for those; `build_graph()` is a pure function and
`rebuild_graph()` is exactly the same call. Later phases (5+) extend a
built graph by resolving capabilities (adding `capability_satisfied_by_*`
edges) and recording evidence -- never by re-deriving requirement/
capability/role nodes, which always come from the plan.

Satisfaction is never a model's bare claim: `mark_satisfied()` requires
at least one recorded evidence entry whose `source` is not
"model_claim" -- deterministic checks, test runs, or human/reviewer
review only (mirrors the platform-wide rule that deterministic
verification has the final vote)."""
import datetime as _dt
import hashlib
import json
import os

from .. import errors

GRAPH_VERSION = 1

NODE_TYPES = ("requirement", "capability", "agent_role", "skill",
             "mcp_tool", "plugin", "deterministic_tool", "test",
             "acceptance_criterion", "evidence")

EDGE_TYPES = ("requirement_requires_capability", "capability_depends_on",
             "capability_satisfied_by_skill", "capability_satisfied_by_mcp",
             "capability_satisfied_by_plugin", "capability_assigned_to_agent",
             "capability_validated_by_check", "check_produces_evidence",
             "evidence_satisfies_acceptance_criterion",
             "capability_conflicts_with", "capability_alternative_to")

STATES = ("unresolved", "resolving", "available", "partially_satisfied",
         "satisfied", "blocked", "waived")

# evidence sources that count toward satisfaction; a bare model assertion
# alone can never satisfy a capability
_VERIFIED_EVIDENCE_SOURCES = {"deterministic_check", "test_run",
                              "human_review", "reviewer_agent"}


class GraphError(errors.PolicyError):
    """Invalid graph operation (bad state, missing reason for a waiver,
    model-only satisfaction claim, unknown node/edge type)."""


def _short_hash(*parts):
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:12]


class CapabilityGraph:
    def __init__(self, project_id, graph_version=GRAPH_VERSION, nodes=None,
                edges=None):
        self.project_id = project_id
        self.graph_version = graph_version
        self.nodes = nodes or {}     # id -> {type, label, attributes}
        self.edges = list(edges or [])   # [{from, to, type, attributes}]

    # -- construction ---------------------------------------------------------
    def add_node(self, node_id, node_type, label, **attributes):
        if node_type not in NODE_TYPES:
            raise GraphError("unknown node type %r" % node_type)
        existing = self.nodes.get(node_id)
        if existing is not None:
            existing["attributes"].update(attributes)
            return node_id
        self.nodes[node_id] = {"type": node_type, "label": label,
                               "attributes": dict(attributes)}
        if node_type == "capability" and "state" not in \
                self.nodes[node_id]["attributes"]:
            self.nodes[node_id]["attributes"]["state"] = "unresolved"
        return node_id

    def add_edge(self, from_id, to_id, edge_type, **attributes):
        if edge_type not in EDGE_TYPES:
            raise GraphError("unknown edge type %r" % edge_type)
        if from_id not in self.nodes or to_id not in self.nodes:
            raise GraphError("edge %s->%s (%s) references an unknown node"
                             % (from_id, to_id, edge_type))
        edge = {"from": from_id, "to": to_id, "type": edge_type,
               "attributes": dict(attributes)}
        if edge not in self.edges:
            self.edges.append(edge)

    # -- lookups ------------------------------------------------------------
    def get_node(self, node_id):
        return self.nodes.get(node_id)

    def nodes_of_type(self, node_type):
        return {nid: n for nid, n in self.nodes.items()
               if n["type"] == node_type}

    def edges_from(self, node_id, edge_type=None):
        return [e for e in self.edges if e["from"] == node_id
               and (edge_type is None or e["type"] == edge_type)]

    def edges_to(self, node_id, edge_type=None):
        return [e for e in self.edges if e["to"] == node_id
               and (edge_type is None or e["type"] == edge_type)]

    # -- satisfaction state ---------------------------------------------------
    def state_of(self, capability_node_id):
        node = self.nodes.get(capability_node_id)
        if node is None or node["type"] != "capability":
            raise GraphError("%r is not a capability node" % capability_node_id)
        return node["attributes"]["state"]

    def set_state(self, capability_node_id, state, *, reason=None):
        if state not in STATES:
            raise GraphError("unknown satisfaction state %r" % state)
        if state == "waived" and not reason:
            raise GraphError("a waiver always requires a reason "
                             "(capability %r)" % capability_node_id)
        node = self.nodes.get(capability_node_id)
        if node is None or node["type"] != "capability":
            raise GraphError("%r is not a capability node"
                             % capability_node_id)
        node["attributes"]["state"] = state
        if reason:
            node["attributes"]["state_reason"] = reason
        node["attributes"]["state_updated_at"] = _dt.datetime.now() \
            .isoformat(timespec="seconds")

    def record_evidence(self, capability_node_id, text, *, source,
                        verified_by=None):
        """Attach an evidence node + check_produces_evidence-style link.
        `source` must be a known kind -- "model_claim" is accepted (a
        model may propose evidence) but is explicitly excluded from what
        `mark_satisfied()` accepts as sufficient."""
        cap = self.nodes.get(capability_node_id)
        if cap is None or cap["type"] != "capability":
            raise GraphError("%r is not a capability node"
                             % capability_node_id)
        evidence_id = "evidence:%s:%s" % (
            capability_node_id, _short_hash(text, source))
        self.add_node(evidence_id, "evidence", text[:200], source=source,
                     verified_by=verified_by,
                     recorded_at=_dt.datetime.now().isoformat(
                         timespec="seconds"))
        for edge in self.edges_from(capability_node_id,
                                    "capability_validated_by_check"):
            self.add_edge(edge["to"], evidence_id, "check_produces_evidence")
        return evidence_id

    def mark_satisfied(self, capability_node_id):
        """Never a bare model claim: requires at least one recorded
        evidence entry from a verified source."""
        evidence_edges = [e for e in self.edges
                          if e["type"] == "check_produces_evidence"]
        cap_test_ids = {e["to"] for e in
                        self.edges_from(capability_node_id,
                                       "capability_validated_by_check")}
        verified = [self.nodes[e["to"]] for e in evidence_edges
                   if e["from"] in cap_test_ids
                   and self.nodes[e["to"]]["attributes"].get("source")
                   in _VERIFIED_EVIDENCE_SOURCES]
        if not verified:
            raise GraphError(
                "capability %r cannot be marked satisfied without at "
                "least one verified evidence entry (deterministic_check, "
                "test_run, human_review, or reviewer_agent) -- a model's "
                "own claim is never sufficient" % capability_node_id)
        self.set_state(capability_node_id, "satisfied")

    def unresolved_capabilities(self):
        return [nid for nid, n in self.nodes_of_type("capability").items()
               if n["attributes"]["state"] in ("unresolved", "resolving",
                                               "blocked")]

    # -- (de)serialisation ------------------------------------------------------
    def to_dict(self):
        return {"project_id": self.project_id,
               "graph_version": self.graph_version,
               "nodes": self.nodes, "edges": self.edges}

    @classmethod
    def from_dict(cls, data):
        return cls(data["project_id"],
                   graph_version=data.get("graph_version", GRAPH_VERSION),
                   nodes=data.get("nodes") or {},
                   edges=data.get("edges") or [])


# -- build (Phase 4): pure function from spec + plan + taxonomy --------------------

def build_graph(spec, capability_plan, taxonomy, *, project_id):
    """Deterministic, rebuildable: the same (spec, plan, taxonomy) always
    produces the same graph. Never calls a model, never touches the
    network. Requirement/capability/agent-role nodes come entirely from
    the CapabilityPlan (Phase 3) -- this never re-infers anything."""
    graph = CapabilityGraph(project_id)
    all_records = (capability_plan["required_capabilities"]
                  + capability_plan["optional_capabilities"])

    section_req_ids = {}

    def requirement_node_for(location, text):
        key = (location, text)
        if key in section_req_ids:
            return section_req_ids[key]
        req_id = "req:%s" % _short_hash(location, text or "")
        graph.add_node(req_id, "requirement",
                      (text or location)[:120],
                      source_location=location, source_text=text)
        section_req_ids[key] = req_id
        return req_id

    # capability nodes first (edges below need both endpoints to exist)
    for record in all_records:
        cap_id = "cap:%s" % record["capability_id"]
        taxonomy_def = taxonomy.get(record["capability_id"]) or {}
        graph.add_node(cap_id, "capability", taxonomy_def.get(
            "name", record["capability_id"]),
            capability_id=record["capability_id"],
            category=taxonomy_def.get("category"),
            risk_level=taxonomy_def.get("risk_level"),
            mandatory=record["mandatory"], confidence=record["confidence"])

    for record in all_records:
        cap_id = "cap:%s" % record["capability_id"]
        if record["source_requirement"] or record["source_location"] not in (
                "dependency_closure", "mandatory_conditions"):
            req_id = requirement_node_for(record["source_location"],
                                          record["source_requirement"])
            graph.add_edge(req_id, cap_id, "requirement_requires_capability",
                          reason=record["reason"],
                          confidence=record["confidence"])

    # dependency / conflict / alternative edges between capability nodes
    for cap_id_raw, deps in (capability_plan.get("dependencies") or {}).items():
        src = "cap:%s" % cap_id_raw
        if src not in graph.nodes:
            continue
        for dep in deps:
            dst = "cap:%s" % dep
            if dst in graph.nodes:
                graph.add_edge(src, dst, "capability_depends_on")

    for conflict in capability_plan.get("conflicts") or []:
        a, b = "cap:%s" % conflict["capability_id"], \
            "cap:%s" % conflict["conflicts_with"]
        if a in graph.nodes and b in graph.nodes:
            graph.add_edge(a, b, "capability_conflicts_with",
                          resolution=conflict["resolution"])

    for record in all_records:
        cap_id = "cap:%s" % record["capability_id"]
        taxonomy_def = taxonomy.get(record["capability_id"]) or {}
        for alt in taxonomy_def.get("alternatives") or []:
            alt_id = "cap:%s" % alt
            if alt_id in graph.nodes:
                graph.add_edge(cap_id, alt_id, "capability_alternative_to")

    # agent roles
    for record in all_records:
        cap_id = "cap:%s" % record["capability_id"]
        taxonomy_def = taxonomy.get(record["capability_id"]) or {}
        for role in taxonomy_def.get("agent_roles") or []:
            role_id = "agent:%s" % role
            graph.add_node(role_id, "agent_role", role)
            graph.add_edge(cap_id, role_id, "capability_assigned_to_agent")

    # deterministic tools (nodes only -- taxonomy has no dedicated edge
    # type for these; they are built-in, not "resolved")
    for record in all_records:
        taxonomy_def = taxonomy.get(record["capability_id"]) or {}
        for tool in taxonomy_def.get("deterministic_tools") or []:
            graph.add_node("tool:%s" % tool, "deterministic_tool", tool)

    # tests (validation checks) -> evidence expectations -> acceptance
    # criteria. Real project-stated acceptance criteria (Phase 1) win
    # over the taxonomy's baseline check names when present.
    project_criteria = []
    ac_section = (spec.get("sections") or {}).get("Acceptance Criteria")
    if ac_section and ac_section.get("present"):
        project_criteria = [line.strip("- ").strip()
                            for line in ac_section["content"].splitlines()
                            if line.strip()]

    for record in all_records:
        cap_id = "cap:%s" % record["capability_id"]
        criteria = project_criteria or record["acceptance_criteria"] or \
            [record["capability_id"] + " meets its baseline requirements"]
        criterion_ids = []
        for i, criterion in enumerate(criteria):
            crit_id = "criterion:%s:%d" % (record["capability_id"], i)
            graph.add_node(crit_id, "acceptance_criterion", criterion[:150])
            criterion_ids.append(crit_id)
        for check in record["acceptance_criteria"]:
            test_id = "test:%s:%s" % (record["capability_id"], check)
            graph.add_node(test_id, "test", check)
            graph.add_edge(cap_id, test_id, "capability_validated_by_check")
        for evidence_text in record["evidence_required"]:
            ev_id = "evidence:%s:%s" % (
                record["capability_id"], _short_hash(evidence_text))
            graph.add_node(ev_id, "evidence", evidence_text[:150],
                          source="expected", recorded_at=None)
            for check in record["acceptance_criteria"]:
                test_id = "test:%s:%s" % (record["capability_id"], check)
                graph.add_edge(test_id, ev_id, "check_produces_evidence")
            for crit_id in criterion_ids:
                graph.add_edge(ev_id, crit_id,
                              "evidence_satisfies_acceptance_criterion")

    return graph


def rebuild_graph(spec, capability_plan, taxonomy, *, project_id):
    """Identical to `build_graph` -- rebuild is always exactly re-derive,
    never an incremental patch, so the graph can never drift from its
    inputs."""
    return build_graph(spec, capability_plan, taxonomy, project_id=project_id)


# -- persistence (project-isolated) --------------------------------------------------

def _graph_path(runtime_dir):
    return os.path.join(str(runtime_dir), "project", "capability-graph.json")


def save_graph(runtime_dir, graph):
    path = _graph_path(runtime_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(graph.to_dict(), fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


def load_graph(runtime_dir):
    path = _graph_path(runtime_dir)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return CapabilityGraph.from_dict(data)
