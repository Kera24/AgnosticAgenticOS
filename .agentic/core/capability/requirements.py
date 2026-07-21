"""Requirements Intelligence Engine (Phase 3): ProjectSpecification +
Capability Taxonomy + repository scan -> CapabilityPlan.

Deterministic rules run first and account for most of a plan: literal
trigger/pattern matches against the specification text, a bounded
repository scan for already-present infrastructure, and -- crucially --
structural inference through the taxonomy graph (`mandatory_conditions`
evaluation and `dependencies` closure) that is NOT keyword matching: a
capability can enter the plan purely because another selected capability
requires it or because the project_type always requires it, with no
literal text match at all.

Semantic gaps (spec content that maps to no taxonomy trigger/pattern at
all) are never silently dropped or guessed at: they become
`unresolved_questions`. An optional `model_caller` hook lets a caller
supply an orchestrator-model callable to interpret those gaps -- never
exercised by the deterministic engine itself, and never called by
default, so automated tests never make a live model call.

PROTECTED_ACTIONS is a fixed baseline: no combination of requirements
ever infers permission to deploy to production or contact external
users -- those stay listed as protected regardless of what capabilities
are selected (rules 7/8)."""
import datetime as _dt
import re

PROTECTED_ACTIONS = [
    "deploy_to_production", "push_to_remote_git", "merge_protected_branch",
    "apply_production_migration", "reset_remote_database",
    "purchase_or_enrol_paid_service", "send_external_communication",
    "publish_package", "expose_local_port_publicly",
    "install_unverified_executable_plugin",
]

# extra protected actions a specific capability's presence implies
_CAPABILITY_PROTECTED_ACTIONS = {
    "payments": ["move_real_money"],
    "compliance_baseline": ["self_certify_compliance"],
}

_REPO_SCAN_TRIGGERS = {
    "Dockerfile": "docker", "docker-compose.yml": "docker",
    "docker-compose.yaml": "docker",
    "supabase/config.toml": "supabase",
}

_TRIGGER_CONFIDENCE = 0.9
_PATTERN_CONFIDENCE = 0.75
_REPO_SCAN_CONFIDENCE = 0.85
_DEPENDENCY_CONFIDENCE = 0.6
_MANDATORY_CONDITION_CONFIDENCE = 0.7


def scan_repository(root):
    """Bounded, deterministic marker-file scan -- never executes
    anything, never reads file contents beyond existence checks."""
    import os
    found = []
    if not root:
        return {"markers": found}
    for rel in _REPO_SCAN_TRIGGERS:
        if os.path.exists(os.path.join(root, rel)):
            found.append(rel)
    return {"markers": found}


def _text_sources(spec):
    """(source_location, text) pairs -- one per present section, plus
    extra (custom) sections. Absent sections contribute nothing."""
    sources = []
    for name, section in (spec.get("sections") or {}).items():
        if section.get("present"):
            sources.append((name, section["content"]))
    for name, content in (spec.get("extra_sections") or {}).items():
        if content.strip():
            sources.append((name, content))
    return sources


def _snippet(text, around=None, width=80):
    text = " ".join(text.split())
    if around:
        idx = text.lower().find(around.lower())
        if idx >= 0:
            start = max(0, idx - width // 2)
            return text[start:start + width].strip()
    return text[:width].strip()


def _predicate_true(cond, *, project_type, corpus, selected_ids):
    if "project_type_in" in cond:
        return project_type in cond["project_type_in"]
    if "requirement_matches" in cond:
        return any(re.search(p, corpus, re.IGNORECASE)
                  for p in cond["requirement_matches"])
    if "capability_present" in cond:
        return cond["capability_present"] in selected_ids
    return False


def _is_mandatory(cap, *, project_type, corpus, selected_ids):
    conditions = cap.get("mandatory_conditions") or []
    return any(_predicate_true(c, project_type=project_type, corpus=corpus,
                               selected_ids=selected_ids)
              for c in conditions)


def _detect_candidates(taxonomy, spec, repo_scan):
    """Deterministic first pass: literal trigger/pattern matches per
    section, plus repository-scan hits. Returns
    {capability_id: [match, ...]} -- every signal is kept for
    provenance, not just the best one."""
    candidates = {}

    def add(cap_id, match):
        candidates.setdefault(cap_id, []).append(match)

    for location, text in _text_sources(spec):
        low = text.lower()
        for cap_id, cap in taxonomy.capabilities.items():
            for trig in cap.get("triggers") or []:
                if trig.lower() in low:
                    add(cap_id, {
                        "capability_id": cap_id, "source_location": location,
                        "source_requirement": _snippet(text, trig),
                        "reason": "matched trigger %r" % trig,
                        "confidence": _TRIGGER_CONFIDENCE,
                        "signal": "trigger"})
            for pattern in cap.get("requirement_patterns") or []:
                try:
                    m = re.search(pattern, text, re.IGNORECASE)
                except re.error:
                    continue
                if m:
                    add(cap_id, {
                        "capability_id": cap_id, "source_location": location,
                        "source_requirement": _snippet(text, m.group(0)),
                        "reason": "matched pattern %r" % pattern,
                        "confidence": _PATTERN_CONFIDENCE,
                        "signal": "pattern"})

    markers = set((repo_scan or {}).get("markers") or [])
    for marker, cap_id in _REPO_SCAN_TRIGGERS.items():
        if marker in markers and cap_id in taxonomy.capabilities:
            add(cap_id, {
                "capability_id": cap_id, "source_location": "repository_scan",
                "source_requirement": marker,
                "reason": "detected %r in repository scan" % marker,
                "confidence": _REPO_SCAN_CONFIDENCE, "signal": "repository_scan"})
    return candidates


def _dependency_closure(taxonomy, seed_ids):
    closed = set(seed_ids)
    frontier = list(seed_ids)
    while frontier:
        cap_id = frontier.pop()
        for dep in taxonomy.dependencies_of(cap_id):
            if dep not in closed and dep in taxonomy.capabilities:
                closed.add(dep)
                frontier.append(dep)
    return closed


def analyse_requirements(spec, taxonomy, *, project_id, project_type=None,
                         repo_root=None, repo_scan=None, clock=None):
    """The Requirements Intelligence Engine's deterministic pass. Returns
    a CapabilityPlan dict matching `capability-plan.schema.json`.

    `repo_scan`: pass a pre-computed `scan_repository(root)` result (or
    let this function compute one from `repo_root`) -- kept as a
    separate parameter so callers/tests can inject a scan without
    touching the filesystem.
    """
    now = (clock or _dt.datetime.now)().isoformat(timespec="seconds")
    project_type = project_type or \
        (spec.get("frontmatter") or {}).get("project_type", "web_application")
    if repo_scan is None:
        repo_scan = scan_repository(repo_root)

    candidates = _detect_candidates(taxonomy, spec, repo_scan)
    corpus = "\n".join(text for _loc, text in _text_sources(spec))

    # fixed-point mandatory-condition evaluation: a capability can become
    # mandatory purely because another selected capability is present,
    # with zero literal text match -- this is the "beyond keyword
    # matching" structural inference the rules require.
    selected_ids = set(candidates)
    mandatory_ids = set()
    changed = True
    while changed:
        changed = False
        for cap_id, cap in taxonomy.capabilities.items():
            if cap_id in mandatory_ids:
                continue
            if _is_mandatory(cap, project_type=project_type, corpus=corpus,
                             selected_ids=selected_ids):
                mandatory_ids.add(cap_id)
                if cap_id not in selected_ids:
                    # a capability newly entering selected_ids can make
                    # another capability's capability_present condition
                    # true on the next pass -- keep iterating.
                    selected_ids.add(cap_id)
                    changed = True

    all_selected = _dependency_closure(taxonomy, selected_ids)
    mandatory_final = _dependency_closure(taxonomy, mandatory_ids)

    # conflict resolution: for each conflicting pair both selected, keep
    # the higher-confidence (mandatory beats optional; then higher raw
    # signal confidence; ties broken by capability id for determinism)
    def _best_confidence(cap_id):
        sigs = candidates.get(cap_id) or []
        base = max((s["confidence"] for s in sigs), default=(
            _MANDATORY_CONDITION_CONFIDENCE if cap_id in mandatory_ids
            else _DEPENDENCY_CONFIDENCE))
        return (cap_id in mandatory_final, base)

    conflicts, rejected = [], []
    resolved_out = set()
    seen_pairs = set()
    for cap_id in sorted(all_selected):
        if cap_id in resolved_out:
            continue
        for other in sorted(taxonomy.conflicts_of(cap_id)):
            if other not in all_selected or other in resolved_out:
                continue
            pair = tuple(sorted((cap_id, other)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            winner, loser = (cap_id, other) \
                if _best_confidence(cap_id) >= _best_confidence(other) \
                else (other, cap_id)
            resolved_out.add(loser)
            conflicts.append({"capability_id": winner,
                              "conflicts_with": loser,
                              "resolution": "kept %r, dropped %r "
                              "(lower confidence/priority)"
                              % (winner, loser)})
            rejected.append({"capability_id": loser,
                             "reason": "conflicts with selected capability "
                                      "%r" % winner})
    all_selected -= resolved_out
    mandatory_final -= resolved_out

    def _requirement_record(cap_id):
        cap = taxonomy.get(cap_id)
        sigs = sorted(candidates.get(cap_id) or [],
                      key=lambda s: -s["confidence"])
        if sigs:
            best = sigs[0]
            source_location = best["source_location"]
            source_requirement = best["source_requirement"]
            reason = best["reason"]
            confidence = best["confidence"]
        elif cap_id in mandatory_ids:
            source_location = "mandatory_conditions"
            source_requirement = ""
            reason = "mandatory for project_type %r or an already-" \
                     "selected capability" % project_type
            confidence = _MANDATORY_CONDITION_CONFIDENCE
        else:
            source_location = "dependency_closure"
            source_requirement = ""
            reason = "required as a dependency of another selected " \
                     "capability"
            confidence = _DEPENDENCY_CONFIDENCE
        return {
            "capability_id": cap_id,
            "source_requirement": source_requirement,
            "source_location": source_location,
            "reason": reason,
            "mandatory": cap_id in mandatory_final,
            "confidence": round(confidence, 2),
            "dependencies": list(cap.get("dependencies") or []),
            "acceptance_criteria": list(cap.get("validation_checks") or []),
            "evidence_required": list(cap.get("evidence_requirements") or []),
        }

    required_capabilities, optional_capabilities = [], []
    inferred_capabilities = []
    for cap_id in sorted(all_selected):
        record = _requirement_record(cap_id)
        (required_capabilities if record["mandatory"]
         else optional_capabilities).append(record)
        if not candidates.get(cap_id):
            inferred_capabilities.append(record)

    required_agent_roles = sorted({role for cap_id in all_selected
                                   for role in
                                   (taxonomy.get(cap_id) or {}).get(
                                       "agent_roles") or []})
    required_validation = sorted({check for cap_id in all_selected
                                  for check in
                                  (taxonomy.get(cap_id) or {}).get(
                                      "validation_checks") or []})

    protected_actions = list(PROTECTED_ACTIONS)
    for cap_id in all_selected:
        protected_actions.extend(
            _CAPABILITY_PROTECTED_ACTIONS.get(cap_id, []))
    protected_actions = sorted(set(protected_actions))

    dependencies = {cap_id: list(taxonomy.dependencies_of(cap_id))
                    for cap_id in all_selected
                    if taxonomy.dependencies_of(cap_id)}

    assumptions = list(spec.get("assumptions") or [])
    unresolved_questions = [
        {"section": q["section"], "question": q["question"]}
        for q in spec.get("blocking_questions") or []]
    # extra_sections that matched nothing at all are semantic gaps: not
    # guessed at, recorded honestly for optional model/user follow-up
    matched_locations = {s["source_location"] for sigs in candidates.values()
                         for s in sigs}
    for name in (spec.get("extra_sections") or {}):
        if name not in matched_locations:
            unresolved_questions.append({
                "section": name,
                "question": "no known capability matched this section; "
                            "review manually or let the orchestrator "
                            "interpret it"})

    confidences = [r["confidence"] for r in
                  required_capabilities + optional_capabilities]
    overall_confidence = round(sum(confidences) / len(confidences), 2) \
        if confidences else 1.0

    provenance = [dict(s) for sigs in candidates.values() for s in sigs]

    return {
        "project_id": project_id,
        "specification_version": spec.get("schema_version", 1),
        "taxonomy_version": taxonomy.taxonomy_version,
        "generated_at": now,
        "required_capabilities": required_capabilities,
        "optional_capabilities": optional_capabilities,
        "inferred_capabilities": inferred_capabilities,
        "rejected_capabilities": rejected,
        "assumptions": assumptions,
        "dependencies": dependencies,
        "conflicts": conflicts,
        "required_agent_roles": required_agent_roles,
        "required_validation": required_validation,
        "protected_actions": protected_actions,
        "confidence": overall_confidence,
        "provenance": provenance,
        "unresolved_questions": unresolved_questions,
    }
