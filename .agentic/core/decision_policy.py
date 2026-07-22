"""Autonomous decision policy: distinguishes genuine human decisions from
reversible technical preferences the architect/conductor can resolve on
their own.

An architect can list an item in `human_decisions` that is not actually a
decision only a human can make -- a reversible implementation preference
(which test runner, which CSS methodology, ...) has no business pausing
an autonomous build. This module classifies each recorded decision and,
for the documented reversible ones, resolves it with a stated default (or
a value inferred from the repository) and a recorded rationale -- never
silently, and never by pausing execution for something a human never
needed to weigh in on.

Only the specific, documented patterns below are ever auto-resolved.
Everything else stays `human_required`: materially consequential,
irreversible, legally/commercially sensitive, credential-dependent, or
otherwise impossible to infer safely."""
import datetime as _dt
import json
import os
import re

from . import projstate

CATEGORY_REVERSIBLE = "reversible_technical_choice"
CATEGORY_HUMAN_REQUIRED = "human_required"

METHOD_AUTONOMOUS_DEFAULT = "autonomous_default"
METHOD_INFERRED_FROM_REPOSITORY = "inferred_from_repository"

RESOLVED_BY = "autonomous_decision_policy"


# -- documented resolvers -------------------------------------------------------

def _find_existing_js_test_framework(repo_root):
    if not repo_root:
        return None
    pkg = os.path.join(repo_root, "package.json")
    if os.path.exists(pkg):
        try:
            with open(pkg, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        deps = dict(data.get("dependencies") or {})
        deps.update(data.get("devDependencies") or {})
        for name, label in (("jest", "Jest"), ("mocha", "Mocha"),
                            ("vitest", "Vitest")):
            if name in deps:
                return label
    for name, label in (("jest.config.js", "Jest"), ("jest.config.ts", "Jest"),
                        (".mocharc.json", "Mocha"), (".mocharc.yml", "Mocha"),
                        ("vitest.config.js", "Vitest"),
                        ("vitest.config.ts", "Vitest")):
        if os.path.exists(os.path.join(repo_root, name)):
            return label
    return None


def _resolve_test_framework(context):
    found = _find_existing_js_test_framework(context.get("repo_root"))
    if found:
        return (found, METHOD_INFERRED_FROM_REPOSITORY,
                "repository already has %s configured" % found, [found])
    return ("Vitest", METHOD_AUTONOMOUS_DEFAULT,
            "documented default for a small JavaScript project with no "
            "existing test tooling installed", [])


_BEM_CLASS_RE = re.compile(r"\.[a-z0-9-]+__[a-z0-9-]+(--[a-z0-9-]+)?", re.I)


def _repo_uses_bem(repo_root):
    if not repo_root:
        return False
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules")]
        for name in files:
            if not name.endswith(".css"):
                continue
            try:
                with open(os.path.join(root, name), encoding="utf-8",
                          errors="replace") as fh:
                    if _BEM_CLASS_RE.search(fh.read()):
                        return True
            except OSError:
                continue
    return False


def _resolve_css_approach(context):
    repo_root = context.get("repo_root")
    if _repo_uses_bem(repo_root):
        return ("BEM methodology", METHOD_INFERRED_FROM_REPOSITORY,
                "existing stylesheets already use BEM-style class names",
                ["BEM class name(s) found in repository CSS"])
    return ("vanilla CSS with CSS custom properties (variables)",
            METHOD_AUTONOMOUS_DEFAULT,
            "documented default: no framework dependency, no existing "
            "methodology detected in the repository", [])


_LEGACY_BROWSER_RE = re.compile(
    r"internet explorer|\bie\s?11\b|legacy browser", re.I)


def _resolve_browser_baseline(context):
    plan_text = context.get("plan_text") or ""
    if _LEGACY_BROWSER_RE.search(plan_text):
        return ("legacy browser support as specified by the project plan",
                METHOD_INFERRED_FROM_REPOSITORY,
                "the project plan explicitly calls out legacy-browser "
                "support", ["PROJECT.md"])
    return ("modern evergreen browsers (ES2015+ baseline)",
            METHOD_AUTONOMOUS_DEFAULT,
            "documented default: no legacy-browser requirement found in "
            "the project plan", [])


# id, pattern, resolver -- ONLY these three documented, reversible
# technical choices are ever auto-resolved (item 7: everything else stays
# human_required).
_RULES = (
    ("test-framework", re.compile(r"test(ing)?\s*framework", re.I),
     _resolve_test_framework),
    ("css-approach", re.compile(r"css\s*(approach|methodology)", re.I),
     _resolve_css_approach),
    ("browser-baseline",
     re.compile(r"browser\s*(compat\w*|baseline)", re.I),
     _resolve_browser_baseline),
)


def classify_decision(text, context=None):
    """Classify one `human_decisions_needed` entry. Returns a dict with
    `category` (reversible_technical_choice | human_required) and, when
    reversible, `resolution_method` (autonomous_default |
    inferred_from_repository), the chosen `value`, a human-readable
    `rationale`, and any supporting `evidence`."""
    context = context or {}
    for rule_id, pattern, resolver in _RULES:
        if pattern.search(text or ""):
            value, method, rationale, evidence = resolver(context)
            return {"decision": text, "rule": rule_id,
                    "category": CATEGORY_REVERSIBLE,
                    "resolution_method": method, "value": value,
                    "rationale": rationale, "evidence": evidence}
    return {"decision": text, "rule": None,
            "category": CATEGORY_HUMAN_REQUIRED, "resolution_method": None,
            "value": None,
            "rationale": "no documented safe default for this decision -- "
                         "treated as materially consequential or "
                         "impossible to infer safely", "evidence": []}


# -- persistence -----------------------------------------------------------------

def _build_context(agentic_dir, repo_root):
    plan_text = ""
    proj_dir = projstate.project_dir(agentic_dir)
    for name in ("PROJECT.md", "architecture.md"):
        path = os.path.join(proj_dir, name)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    plan_text += fh.read()
            except OSError:
                pass
    return {"repo_root": repo_root, "plan_text": plan_text}


def auto_resolve_reversible_decisions(agentic_dir, repo_root=None):
    """Scan `decisions.yaml`'s `human_decisions_needed` and resolve every
    reversible technical choice with its documented default (or a value
    inferred from the repository). Persists the decision + rationale +
    evidence into `decided`, removes it from `human_decisions_needed`,
    and resolves its matching blocker(s) -- never pauses execution, never
    touches a decision this policy doesn't recognise. Returns the list of
    newly-resolved decision records (empty if nothing changed)."""
    if not projstate.exists(agentic_dir):
        return []
    doc = projstate.read_yaml(agentic_dir, "decisions.yaml",
                              {"human_decisions_needed": [], "decided": []})
    needed = list(doc.get("human_decisions_needed") or [])
    if not needed:
        return []
    context = _build_context(agentic_dir, repo_root)
    decided = list(doc.get("decided") or [])
    remaining = []
    newly_resolved = []
    for text in needed:
        result = classify_decision(text, context)
        if result["category"] != CATEGORY_REVERSIBLE:
            remaining.append(text)
            continue
        record = dict(result)
        record["resolved_at"] = _dt.datetime.now().isoformat(
            timespec="seconds")
        record["resolved_by"] = RESOLVED_BY
        decided.append(record)
        newly_resolved.append(record)
    if not newly_resolved:
        return []
    doc["human_decisions_needed"] = remaining
    doc["decided"] = decided
    projstate.write_yaml(agentic_dir, "decisions.yaml", doc)
    _resolve_matching_blockers(agentic_dir,
                               [r["decision"] for r in newly_resolved])
    return newly_resolved


def _resolve_matching_blockers(agentic_dir, decision_texts):
    """Resolve the project-level (task=None) human blockers that were
    recorded verbatim for each now-auto-resolved decision. Never touches
    a blocker belonging to a task, or any other project-level blocker."""
    blockers = projstate.read_yaml(agentic_dir, "blockers.yaml",
                                   {"blockers": []})
    changed = False
    texts = set(decision_texts)
    for b in blockers.get("blockers", []):
        if b.get("task") is None and not b.get("resolved") and \
                b.get("reason") in texts:
            b["resolved"] = True
            changed = True
    if changed:
        projstate.write_yaml(agentic_dir, "blockers.yaml", blockers)
