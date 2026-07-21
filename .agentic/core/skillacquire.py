"""Risk-Based Skill Acquisition (Phase 6).

Extends `core.skillreg` (the single installed-skill authority) and
`core.skillmarket` (discover -> quarantine -> evaluate -> approve/
reject -> rollback, all already real and unchanged here) with:

- the LEVEL 0-4 trust-tier vocabulary the design calls for
- hook and MCP-server declaration detection (skillreg's static scan only
  ever covered scripts/suspicious tokens -- this module adds the rest)
- permission-expansion detection (baseline: read-only)
- a DETERMINISTIC automatic-approval ENGINE: `classify_trust_level` and
  `auto_approve_if_eligible` are pure functions over structured signals.
  A model may recommend a candidate (SkillCurator, untouched, still has
  no approve/install surface) but can never itself decide LEVEL 2 --
  only this policy can, and only when every criterion is independently
  true.
- generating a project-local, reviewer-gated fallback skill from the
  capability taxonomy's own (trusted, shipped) documentation when no
  safe external candidate exists -- deterministic templating, never a
  model call, never auto-approved, never published to the shared
  registry.

LEVEL 0 metadata_only   -- a discover() search result; nothing downloaded
LEVEL 1 quarantined     -- copied into isolated storage, awaiting review
LEVEL 2 auto_approved   -- meets every deterministic low-risk criterion
LEVEL 3 explicitly_approved -- an administrator approved it despite not
                               meeting LEVEL 2 (scripts, hooks, etc.)
LEVEL 4 blocked         -- unsafe or unverifiable; never installable
"""
import os
import re

from . import errors
from .skillmarket import SkillError, SkillMarket, _injection_findings
from .skillreg import SkillRegistry, _scan

LEVEL_METADATA_ONLY = 0
LEVEL_QUARANTINED = 1
LEVEL_AUTO_APPROVED = 2
LEVEL_EXPLICITLY_APPROVED = 3
LEVEL_BLOCKED = 4

LEVEL_NAMES = {LEVEL_METADATA_ONLY: "metadata_only",
              LEVEL_QUARANTINED: "quarantined",
              LEVEL_AUTO_APPROVED: "auto_approved",
              LEVEL_EXPLICITLY_APPROVED: "explicitly_approved",
              LEVEL_BLOCKED: "blocked"}

DEFAULT_POLICY = {
    "auto_approve_low_risk": True,
    "require_pinned_revision": True,
    "require_checksum": True,
    "allow_executables": False,
    "allow_hooks": False,
    "allow_permission_expansion": False,
    "allow_unknown_licence": False,
    "trusted_source_types": ("builtin", "internal", "local_index",
                             "mirror_index", "project_generated"),
}

BASELINE_PERMISSIONS = {"read"}
BINARY_EXTENSIONS = (".exe", ".dll", ".so", ".dylib", ".bin")
_HOOK_NAME_RE = re.compile(r"(?i)(^|[/_-])hooks?\.(ya?ml|json)$")
_MCP_NAME_RE = re.compile(r"(?i)(^|[/_.-])mcp([_-]?server)?\.(ya?ml|json)$")

MAX_ACQUISITION_CANDIDATES = 5


class SkillAcquisitionError(errors.PolicyError):
    """A skill-acquisition invariant was violated (never used to mask an
    ordinary quarantine/evaluate failure -- those raise SkillError)."""


def policy_from_cfg(cfg):
    merged = dict(DEFAULT_POLICY)
    merged.update((cfg.get("skills") or {}).get("trust") or {})
    return merged


# -- static analysis additions: hooks, MCP declarations, binaries, permissions ------

def detect_hooks(directory):
    found = []
    for base, _dirs, names in os.walk(directory):
        for name in names:
            rel = os.path.relpath(os.path.join(base, name), directory) \
                .replace("\\", "/")
            if _HOOK_NAME_RE.search(name) or "/hooks/" in ("/" + rel):
                found.append(rel)
    return sorted(set(found))


def detect_mcp_declarations(directory):
    found = []
    for base, _dirs, names in os.walk(directory):
        for name in names:
            if _MCP_NAME_RE.search(name):
                found.append(os.path.relpath(os.path.join(base, name),
                                             directory).replace("\\", "/"))
    return sorted(set(found))


def detect_binaries(directory):
    found = []
    for base, _dirs, names in os.walk(directory):
        for name in names:
            if name.lower().endswith(BINARY_EXTENSIONS):
                found.append(os.path.relpath(os.path.join(base, name),
                                             directory).replace("\\", "/"))
    return sorted(set(found))


def scan_extended(directory):
    """The hook/MCP/binary signals skillreg's own `_scan()` never
    covered -- computed once, reused by classification."""
    return {"hooks": detect_hooks(directory),
           "mcp_declarations": detect_mcp_declarations(directory),
           "binaries": detect_binaries(directory)}


def analyse_permissions(record):
    requested = set(record.get("permissions") or ["read"])
    expansion = bool(requested - BASELINE_PERMISSIONS)
    return requested, expansion


# -- LEVEL 0-4 classification (deterministic policy, never a model) ----------------

def classify_trust_level(record, *, scan=None, policy=None):
    """`record`: a skillmarket catalog record. `scan`: this module's
    `scan_extended()` result for the quarantined directory (required to
    reach LEVEL 2/3; a pre-quarantine record without a scan can only ever
    be LEVEL 0 or LEVEL 4). Returns (level, reasons)."""
    policy = policy or DEFAULT_POLICY
    state = record.get("state")
    if state in (None, "discovered"):
        return LEVEL_METADATA_ONLY, ["search result only; not yet "
                                     "quarantined"]
    eval_result = record.get("evaluation_result") or {}
    if eval_result.get("injection_findings"):
        return LEVEL_BLOCKED, ["prompt-injection pattern(s) found: %s"
                               % eval_result["injection_findings"][:3]]
    if state == "rejected":
        return LEVEL_BLOCKED, ["candidate was rejected (checksum mismatch "
                               "or explicit reject)"]
    if policy["require_checksum"] and not record.get("checksum"):
        return LEVEL_BLOCKED, ["no recorded checksum"]
    if policy["require_pinned_revision"] and not record.get(
            "pinned_revision"):
        return LEVEL_BLOCKED, ["revision not pinned"]

    if scan is None:
        # never assume "clean" for hooks/MCP/binaries when the extended
        # scan wasn't actually run -- absence of evidence is not
        # evidence of absence; LEVEL 2/3 both require it
        return LEVEL_QUARANTINED, ["extended scan (hooks/MCP-declarations/"
                                   "binaries) was not performed; cannot "
                                   "auto-approve without it"]
    scripts = record.get("scripts") or []
    _permissions, expansion = analyse_permissions(record)
    licence = record.get("licence")
    licence_ok = bool(licence) and licence != "unknown" or \
        policy["allow_unknown_licence"]
    source_trusted = record.get("source_type") in \
        policy["trusted_source_types"]

    checks = {
        "source_trusted": source_trusted,
        "revision_pinned": bool(record.get("pinned_revision")),
        "checksum_recorded": bool(record.get("checksum")),
        "licence_accepted": licence_ok,
        "no_scripts": not scripts or policy["allow_executables"],
        "no_hooks": not scan["hooks"] or policy["allow_hooks"],
        "no_mcp_declarations": not scan["mcp_declarations"],
        "no_binaries": not scan["binaries"],
        "no_permission_expansion": not expansion or
        policy["allow_permission_expansion"],
        "static_scan_clean": not eval_result.get("script_findings"),
        "injection_scan_clean": not eval_result.get("injection_findings"),
        "fixture_evaluation_passed": eval_result.get("verdict") ==
        "recommend" or state == "approved",
    }
    failing = [name for name, ok in checks.items() if not ok]

    if not failing:
        return LEVEL_AUTO_APPROVED, ["every LEVEL 2 criterion satisfied"]
    if state == "approved":
        return LEVEL_EXPLICITLY_APPROVED, [
            "approved despite: %s (administrator decision, never "
            "automatic)" % ", ".join(failing)]
    return LEVEL_QUARANTINED, ["awaiting review -- failing: %s"
                              % ", ".join(failing)]


def auto_approve_if_eligible(market, skill_id, *, policy=None):
    """Runs `classify_trust_level` against the CURRENT quarantined+
    evaluated state and, only for LEVEL 2 with
    `auto_approve_low_risk` enabled, calls `market.approve()` itself and
    enables it. Never consults a model. Returns
    (approved: bool, level: int, reasons: [str]).

    An update to an ALREADY-INSTALLED skill is never auto-approved, no
    matter how low-risk it now scores -- a version change always waits
    for a human (`skills compare` then `skills approve`), since low risk
    at first install says nothing about whether a new revision still is."""
    policy = policy or DEFAULT_POLICY
    record = market.candidate(skill_id)
    if record["state"] == "update_available" or record.get(
            "previous_revision"):
        return False, LEVEL_QUARANTINED, [
            "candidate is an update to an already-installed skill; "
            "updates always require explicit human approval "
            "('skills compare' then 'skills approve'), never automatic"]
    if record["state"] != "quarantined":
        return False, LEVEL_QUARANTINED, ["candidate is not quarantined "
                                          "(state=%r)" % record["state"]]
    directory = os.path.join(market.paths["quarantine"], skill_id)
    scan = scan_extended(directory) if os.path.isdir(directory) else None
    level, reasons = classify_trust_level(record, scan=scan, policy=policy)
    if level == LEVEL_AUTO_APPROVED and policy["auto_approve_low_risk"]:
        market.approve(skill_id)
        market.registry.enable(skill_id)
        return True, level, ["auto-approved by deterministic policy: "
                             + reasons[0]]
    return False, level, reasons


# -- end-to-end acquisition for one capability (the Phase 5 registry_search hook) ---

def acquire_skill_for_capability(market, cap_def, *, policy=None,
                                 project_id=None, runtime_dir=None):
    """Search -> quarantine -> evaluate -> (auto-)approve, for every
    candidate a `discover()` call turns up for this capability. Returns
    ResolutionCandidate-shaped dicts (see core.capability.resolver) so
    this can be passed straight through as Phase 5's `registry_search`
    hook. Never touches the network itself (discover() only reads
    locally-configured/pre-mirrored sources, per skillmarket's existing
    no-network design).

    If nothing safe is found and the capability genuinely needs a skill
    (it declares `suggested_skills`), a project-local, reviewer-gated
    fallback skill is generated and returned as an additional candidate
    -- never auto-approved, never counted as "available" until a human
    reviews it."""
    policy = policy or DEFAULT_POLICY
    if not cap_def.get("suggested_skills"):
        # taxonomy's explicit signal that this capability is the kind of
        # thing an external skill satisfies -- without it, running
        # discover/quarantine/evaluate for every capability during a
        # resolve pass would be pure overhead with no useful result
        return []
    query = " ".join((cap_def.get("suggested_skills") or [])
                     + (cap_def.get("triggers") or []))
    discovery = market.discover(query)
    out = []
    for meta in discovery["candidates"][:MAX_ACQUISITION_CANDIDATES]:
        skill_id = meta["id"]
        try:
            market.quarantine(skill_id)
        except SkillError as exc:
            out.append(_result(cap_def["id"], meta, "rejected", "high",
                               str(exc)))
            continue
        market.evaluate(skill_id)
        approved, level, reasons = auto_approve_if_eligible(
            market, skill_id, policy=policy)
        record = market.candidate(skill_id)
        status = "available" if approved else (
            "rejected" if level == LEVEL_BLOCKED else "unavailable")
        out.append(_result(cap_def["id"], record, status,
                           "low" if approved else
                           ("high" if level == LEVEL_BLOCKED else "medium"),
                           "; ".join(reasons), trust_level=level))

    if not any(c["status"] == "available" for c in out) and \
            cap_def.get("suggested_skills") and runtime_dir:
        generated = generate_project_local_skill(
            cap_def, runtime_dir, project_id=project_id)
        out.append(generated)
    return out


def _result(capability_id, record, status, risk, reason, trust_level=None):
    return {
        "capability_id": capability_id, "type": "skill",
        "source": record.get("source_type")
        or record.get("source", "external_registry"),
        "name": record.get("id", record.get("name", "candidate")),
        "revision": record.get("pinned_revision") or
        record.get("current_revision"), "risk": risk,
        "status": status, "rejection_reason": reason if status != "available"
        else None,
        "trust": 1.0 if status == "available" else 0.3,
        "quality_score": 0.7, "maintenance_score": 0.6,
        "trust_level": trust_level,
    }


# -- generated project-local fallback skill (never auto-approved) ------------------

def _render_skill_md(cap_def):
    lines = ["---", "name: %s (generated)" % cap_def.get("name",
                                                          cap_def["id"]),
            "description: Project-local fallback skill generated from "
            "the capability taxonomy -- pending reviewer validation.",
            "generated: true", "source: project_generated", "---", "",
            "# %s" % cap_def.get("name", cap_def["id"]), "",
            cap_def.get("description", ""), "",
            "## Validation checks", ""]
    for check in cap_def.get("validation_checks") or []:
        lines.append("- %s" % check)
    lines += ["", "## Evidence expected", ""]
    for evidence in cap_def.get("evidence_requirements") or []:
        lines.append("- %s" % evidence)
    lines += ["", "> This skill was generated automatically from platform "
             "documentation because no safe external skill was found. It "
             "has NOT been reviewed and must not be treated as "
             "authoritative until a human validates it."]
    return "\n".join(lines) + "\n"


def generate_project_local_skill(cap_def, runtime_dir, *, project_id=None):
    """Deterministic templating from the (trusted, shipped) capability
    taxonomy only -- never a model call. Written under the PROJECT's own
    runtime dir, never the shared/builtin skills registry, and returned
    with `status="unavailable"` -- it exists and is ready for review, but
    never silently satisfies the capability on its own."""
    skill_id = "generated-%s" % cap_def["id"]
    directory = os.path.join(str(runtime_dir), "skills", "generated",
                             skill_id)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "SKILL.md"), "w", encoding="utf-8",
             newline="\n") as fh:
        fh.write(_render_skill_md(cap_def))
    checksum = SkillRegistry.compute_checksum(directory)
    return {
        "capability_id": cap_def["id"], "type": "skill",
        "source": "project_generated",
        "name": skill_id, "revision": "generated-v1", "risk": "low",
        "status": "unavailable",
        "rejection_reason": "generated project-local skill pending "
                            "reviewer validation (never auto-approved, "
                            "never published)",
        "trust": 0.3, "quality_score": 0.4, "maintenance_score": 0.3,
        "trust_level": LEVEL_QUARANTINED,
        "path": directory, "checksum": checksum, "project_id": project_id,
    }
