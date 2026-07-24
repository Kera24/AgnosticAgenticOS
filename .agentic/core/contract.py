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

from . import bootstrap_gate

CONTRACT_VERSION = "1.0"

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
