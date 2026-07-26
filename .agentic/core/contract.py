"""Canonical task contract (Phase 1.A).

The one versioned data shape every layer that touches a task should
read from: architect (via the backlog task it emits), conductor (via
the work order it emits), worker, security policy, the filesystem
capability layer, the deterministic gate, the QA reviewer, the recovery
engine, and the native (and future Orca) execution engine.

This is a PROJECTION, not a new persisted file: it is built on demand
from the existing backlog task (backlog.yaml) and the conductor's work
order (work-order.json) -- both already the source of truth. No
migration of existing project state is required; a project started
before this module existed produces a perfectly valid contract from its
existing task/order shape (missing fields simply come back empty)."""
import datetime as _dt
import hashlib
import json
import re

from . import bootstrap_gate, gitops

CONTRACT_VERSION = "1.0"

# Best-effort extraction of file/directory-like tokens from free prose (an
# acceptance criterion) -- anything with a path separator, or a bare name
# with a plausible file extension. Deliberately conservative: ordinary
# words with no separator and no extension never match.
_PATH_LIKE_RE = re.compile(
    r"[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+/?|"
    r"\b[A-Za-z0-9_\-]+\.[A-Za-z0-9]{1,8}\b")

REQUIRED_CONTRACT_FIELDS = (
    "contract_version", "project_id", "task_id", "kind", "objective",
    "dependencies", "required_inputs", "required_outputs", "allowed_paths",
    "prohibited_paths", "required_capabilities", "execution_budget",
    "deterministic_checks", "acceptance_criteria", "decision_classifications",
    "risk", "rollback_strategy", "completion_evidence_requirements",
)


def build_task_contract(task, order, project_id, run_id=None,
                        decision_classifications=None):
    """Assemble the canonical contract from an existing backlog task +
    conductor work order. Typed `required_outputs` entries come from
    `bootstrap_gate.normalize_expected_entry` -- the same normalisation
    the structural gate itself uses, so the contract's acceptance
    contract and the gate's evaluation of it can never drift apart."""
    task = task or {}
    order = order or {}
    required_outputs = [bootstrap_gate.normalize_expected_entry(e)
                        for e in task.get("expected_paths") or []]
    security_relevant = bool(task.get("security_relevant"))
    evidence = ["deterministic_or_structural_gate_pass", "independent_qa_pass"]
    if security_relevant:
        evidence.append("security_review_pass")
    return {
        "contract_version": CONTRACT_VERSION,
        "project_id": project_id,
        "task_id": task.get("id"),
        "run_id": run_id,
        "kind": task.get("kind"),
        "objective": order.get("item") or task.get("description") or "",
        "dependencies": list(task.get("dependencies") or []),
        "required_inputs": list(order.get("required_inputs") or []),
        "required_outputs": required_outputs,
        "allowed_paths": list(order.get("allowed_paths") or []),
        "prohibited_paths": list(order.get("forbidden_paths") or []),
        "required_capabilities": list(order.get("required_capabilities") or []),
        "execution_budget": {
            "maximum_changed_lines": order.get("maximum_changed_lines"),
            "expected_size": task.get("expected_size") or "medium",
        },
        "deterministic_checks": list(task.get("deterministic_checks") or []),
        "acceptance_criteria": list(task.get("acceptance_criteria")
                                    or order.get("acceptance_criteria") or []),
        "decision_classifications": list(decision_classifications or []),
        "risk": order.get("risk") or task.get("risk") or "medium",
        "rollback_strategy": "revert_task_worktree_and_retry",
        "completion_evidence_requirements": evidence,
        # The conductor's OWN declared expected_outputs (work-order schema
        # field, historically never read by anything downstream) -- kept
        # here, on the ONE compiled contract, purely as evidence for
        # `find_work_order_divergences`. This is never merged into
        # `required_outputs`: the backlog task's typed expected_paths stay
        # the single canonical source the conductor's work order must stay
        # within, never a place the conductor can unilaterally expand.
        "work_order_expected_outputs": list(order.get("expected_outputs")
                                           or []),
        "built_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }


def validate_contract_shape(contract):
    """Fast, dependency-free structural check -- every required field
    present and the version supported. The JSON-Schema file documents
    the same shape for external/Orca consumers; this is what the
    preflight actually runs on every cycle."""
    problems = []
    contract = contract or {}
    for key in REQUIRED_CONTRACT_FIELDS:
        if key not in contract:
            problems.append("missing contract field: %s" % key)
    if contract.get("contract_version") != CONTRACT_VERSION:
        problems.append("unsupported contract_version: %r"
                        % contract.get("contract_version"))
    if "required_outputs" in contract:
        for entry in contract["required_outputs"] or []:
            if not isinstance(entry, dict) or not entry.get("path"):
                problems.append(
                    "malformed required_outputs entry: %r" % (entry,))
    return problems


def contract_hash(contract):
    """Stable identity over the parts of the contract every consumer must
    agree on: required_outputs/allowed_paths/prohibited_paths/
    acceptance_criteria. Used to detect when a task's compiled contract
    has materially changed (e.g. after a canonical-contract-divergence
    migration) so dependent prompt/context-cache entries can be
    invalidated (item 6) instead of silently going stale."""
    contract = contract or {}
    payload = {
        "required_outputs": contract.get("required_outputs") or [],
        "allowed_paths": sorted(contract.get("allowed_paths") or []),
        "prohibited_paths": sorted(contract.get("prohibited_paths") or []),
        "acceptance_criteria": contract.get("acceptance_criteria") or [],
    }
    text = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _covered_by_required_output(path, required_outputs, required_by_path):
    if path in required_by_path:
        return True
    return any(e.get("type") == "glob" and gitops.match_pattern(path, e["path"])
              for e in required_outputs)


def _covered_by_criterion(path, required_outputs, required_by_path):
    """Looser than `_covered_by_required_output`: an acceptance criterion
    naming a path UNDER an already-required directory is adequately
    covered by that directory's own typed entry (item 3: "directory
    paths and descendant patterns must be handled correctly") -- only a
    work-order `expected_outputs` entry needs its OWN distinct typed
    entry (see `find_work_order_divergences`)."""
    if _covered_by_required_output(path, required_outputs, required_by_path):
        return True
    stripped = path.rstrip("/")
    for entry in required_outputs:
        if entry.get("type") == "directory" and (
                stripped == entry["path"] or
                stripped.startswith(entry["path"] + "/")):
            return True
    return False


def find_work_order_divergences(contract):
    """Item 3's HARD contract-consistency gate: every work-order declared
    expected output must already exist in the compiled contract -- the
    conductor works WITHIN the canonical contract, never expands it
    unilaterally (see `build_task_contract`'s `work_order_expected_outputs`).
    Returns a list of problem strings; empty means consistent. A no-op
    (and zero cost) for the overwhelming majority of tasks, which never
    populate a work order's `expected_outputs` field at all."""
    contract = contract or {}
    required_outputs = contract.get("required_outputs") or []
    required_by_path = {e["path"]: e for e in required_outputs
                        if isinstance(e, dict) and e.get("path")}
    problems = []
    for raw in contract.get("work_order_expected_outputs") or []:
        entry = bootstrap_gate.normalize_expected_entry(raw)
        if not _covered_by_required_output(entry["path"], required_outputs,
                                           required_by_path):
            problems.append(
                "work-order expected output %r is not present in the "
                "compiled task contract's required_outputs" % entry["path"])
    return problems


def find_acceptance_criteria_gaps(contract):
    """Item 3's SOFT evidence check: every acceptance criterion that
    names a file/directory-like path SHOULD map to a typed required
    output. Deliberately never wired into preflight's hard gate --
    acceptance-criteria prose is free text an architect/human wrote, and
    a plausible-looking path mention inside it is evidence worth
    persisting alongside the compiled contract, never grounds to refuse
    dispatch on a heuristic text match alone. Returns a list of gap
    strings; empty means every mention is covered."""
    contract = contract or {}
    required_outputs = contract.get("required_outputs") or []
    required_by_path = {e["path"]: e for e in required_outputs
                        if isinstance(e, dict) and e.get("path")}
    gaps = []
    for text in contract.get("acceptance_criteria") or []:
        for token in _PATH_LIKE_RE.findall(text or ""):
            token = token.strip().rstrip(".,;:)")
            if not token:
                continue
            if not _covered_by_criterion(token, required_outputs,
                                         required_by_path):
                gaps.append(
                    "acceptance criterion %r references %r, which is not "
                    "covered by any typed required output" % (text, token))
    return gaps
