"""Plugin Capability Resolution (Phase 7).

A "plugin" bundles multiple component kinds together -- skills, hooks,
MCP server declarations, LSP integrations, executables. This module
decomposes a plugin directory into those components and evaluates each
one INDEPENDENTLY, through the exact same pipeline a bare component of
that kind would go through (Phase 6's skill acquisition for skill
components, Phase 7's MCP resolver for MCP components). A plugin is
never approved or rejected as a monolithic unit:

- an unsafe executable or hook inside an otherwise-useful plugin never
  blocks the safe skill component from being used;
- a legitimate-looking bundle never gets a free pass for its unsafe
  parts because the bundle overall looks legitimate;
- a plugin never receives more authority than the specific components a
  task actually needs -- each component is approved (or not) on its own
  merits, at its own trust level.

Hooks and executables have no reviewed-execution pathway in this
platform yet, so they are ALWAYS classified as needing explicit human
review here -- never auto-approved, regardless of how the rest of the
bundle scores.
"""
import os

from . import errors
from .skillacquire import (LEVEL_AUTO_APPROVED, LEVEL_BLOCKED,
                           classify_trust_level, detect_binaries,
                           detect_hooks, detect_mcp_declarations)
from .skillreg import SCRIPT_EXTENSIONS, _scan

COMPONENT_KINDS = ("skill", "hook", "mcp_server", "lsp", "executable")

_LSP_NAME_HINTS = ("lsp", "language-server", "language_server")


class PluginError(errors.PolicyError):
    """A plugin-decomposition invariant was violated."""


def decompose_plugin(directory):
    """Deterministic, filesystem-only classification -- no model call.
    Returns {"skills": [...], "hooks": [...], "mcp_servers": [...],
    "lsp": [...], "executables": [...]}, each a list of relative paths
    (skills: subdirectory names containing a SKILL.md)."""
    if not os.path.isdir(directory):
        raise PluginError("plugin directory does not exist: %s" % directory)
    components = {"skills": [], "hooks": [], "mcp_servers": [], "lsp": [],
                 "executables": []}
    for entry in sorted(os.listdir(directory)):
        full = os.path.join(directory, entry)
        if os.path.isdir(full) and os.path.exists(
                os.path.join(full, "SKILL.md")):
            components["skills"].append(entry)

    components["hooks"] = detect_hooks(directory)
    components["mcp_servers"] = detect_mcp_declarations(directory)
    components["executables"] = detect_binaries(directory)
    for base, _dirs, names in os.walk(directory):
        for name in names:
            rel = os.path.relpath(os.path.join(base, name), directory) \
                .replace("\\", "/")
            if name.lower().endswith(SCRIPT_EXTENSIONS) and \
                    rel not in components["executables"]:
                components["executables"].append(rel)
            if any(hint in name.lower() for hint in _LSP_NAME_HINTS) and \
                    name.lower().endswith((".json", ".yaml", ".yml")):
                components["lsp"].append(rel)
    for key in components:
        components[key] = sorted(set(components[key]))
    return components


def _skill_component_scan(directory, skill_subdir):
    """Reuse skillreg's own static scan for a skill component exactly as
    it would run on any standalone skill -- no separate, weaker rules
    for "it came from a plugin"."""
    path = os.path.join(directory, skill_subdir)
    files, scripts, findings = _scan(path)
    return {"files": files, "scripts": scripts, "findings": findings}


def evaluate_plugin_components(directory, *, policy=None):
    """Evaluate every decomposed component independently. Returns
    {"components": <decompose_plugin() output>,
     "decisions": {component_path: {"kind", "level"/"status", "reasons"}}}
    Never grants a component authority a bare skill/MCP candidate of the
    same kind wouldn't also get -- this is composition, not a new,
    looser policy path."""
    components = decompose_plugin(directory)
    decisions = {}

    for skill_dir in components["skills"]:
        scan = _skill_component_scan(directory, skill_dir)
        full = os.path.join(directory, skill_dir)
        record = {
            "state": "quarantined",
            "pinned_revision": "plugin-component",
            "checksum": "unverified",   # plugin components are not
            # individually checksum-pinned by skillmarket's registry
            # flow; they are evaluated in place, always at most
            # LEVEL_QUARANTINED until formally quarantined on their own
            "licence": "unknown", "scripts": scan["scripts"],
            "permissions": ["read"], "source_type": "plugin_component",
            "evaluation_result": {
                "script_findings": scan["findings"],
                "injection_findings": [],
                "verdict": "review_findings"},
        }
        extended_scan = {
            "hooks": [h for h in components["hooks"]
                     if h.startswith(skill_dir + "/")],
            "mcp_declarations": [m for m in components["mcp_servers"]
                                 if m.startswith(skill_dir + "/")],
            "binaries": [b for b in components["executables"]
                        if b.startswith(skill_dir + "/")],
        }
        level, reasons = classify_trust_level(record, scan=extended_scan,
                                              policy=policy)
        # a plugin's skill component can be recommended but never
        # silently auto-installed by THIS function -- installation still
        # goes through skillmarket's real discover/quarantine/approve
        # pipeline once a human (or Phase 6's acquisition hook, for a
        # component copied out as its own candidate) chooses to pursue it
        status = "available" if level == LEVEL_AUTO_APPROVED else (
            "rejected" if level == LEVEL_BLOCKED else "unavailable")
        decisions[skill_dir] = {"kind": "skill", "level": level,
                                "status": status, "reasons": reasons}

    for hook in components["hooks"]:
        decisions[hook] = {
            "kind": "hook", "level": LEVEL_BLOCKED, "status": "rejected",
            "reasons": ["hooks have no reviewed-execution pathway; "
                       "always requires explicit human review"]}

    for executable in components["executables"]:
        decisions[executable] = {
            "kind": "executable", "level": LEVEL_BLOCKED,
            "status": "rejected",
            "reasons": ["executable components are never auto-approved"]}

    for mcp_decl in components["mcp_servers"]:
        decisions[mcp_decl] = {
            "kind": "mcp_server", "level": None, "status": "unavailable",
            "reasons": ["MCP server declarations inside a plugin require "
                       "the same resolution as a standalone MCP "
                       "capability (core.mcpresolve) -- never configured "
                       "automatically from bundle content alone"]}

    for lsp_file in components["lsp"]:
        decisions[lsp_file] = {
            "kind": "lsp", "level": LEVEL_AUTO_APPROVED,
            "status": "available",
            "reasons": ["read-only language-server configuration; "
                       "no execution authority granted"]}

    return {"components": components, "decisions": decisions}


def safe_components(evaluation):
    """The subset of a plugin a task may actually use -- exactly the
    components that scored available, nothing implied by the rest of
    the bundle."""
    return {path: d for path, d in evaluation["decisions"].items()
           if d["status"] == "available"}
