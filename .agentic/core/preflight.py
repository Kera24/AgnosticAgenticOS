"""Feasibility preflight (Phase 1.C).

Runs entirely in code, BEFORE any model is invoked for a task attempt.
Never consumes model/provider capacity for a `platform_invalid` result:
callers are expected to short-circuit immediately on that result rather
than calling the coder."""
import os

from . import bootstrap_gate, contract as contract_mod, decision_policy, gitops

RESULT_FEASIBLE = "feasible"
RESULT_AUTO_REPAIRED = "auto_repaired"
RESULT_REPLAN_REQUIRED = "replan_required"
RESULT_DEPENDENCY_WAIT = "dependency_wait"
RESULT_CREDENTIAL_REQUIRED = "credential_required"
RESULT_HUMAN_REQUIRED = "human_required"
RESULT_PLATFORM_INVALID = "platform_invalid"

RESULTS = (RESULT_FEASIBLE, RESULT_AUTO_REPAIRED, RESULT_REPLAN_REQUIRED,
          RESULT_DEPENDENCY_WAIT, RESULT_CREDENTIAL_REQUIRED,
          RESULT_HUMAN_REQUIRED, RESULT_PLATFORM_INVALID)

# Never consumes model capacity: callers must short-circuit on these
# without invoking the coder.
NO_CAPACITY_RESULTS = (RESULT_DEPENDENCY_WAIT, RESULT_CREDENTIAL_REQUIRED,
                       RESULT_HUMAN_REQUIRED, RESULT_PLATFORM_INVALID,
                       RESULT_REPLAN_REQUIRED)


def _verdict(result, checks, note=None):
    return {"result": result, "checks": checks,
            "consumes_capacity": result not in NO_CAPACITY_RESULTS,
            "note": note}


def _find_contradictions(contract):
    problems = []
    prohibited = contract.get("prohibited_paths") or []
    for entry in contract.get("required_outputs") or []:
        path = entry.get("path", "")
        if path and gitops.matches_any(path, prohibited):
            problems.append(
                "required output %r also matches a prohibited path" % path)
    return problems


def run_preflight(contract, task, backlog, worktree, project_root,
                  decisions_needed=None, capacity_decision=None,
                  backend=None, inventory=None):
    """Returns {"result": <one of RESULTS>, "checks": [...],
    "consumes_capacity": bool, "note": ...}."""
    checks = []

    def check(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        return ok

    problems = contract_mod.validate_contract_shape(contract)
    if not check("contract_parses", not problems, "; ".join(problems)
                or "contract shape is valid"):
        return _verdict(RESULT_PLATFORM_INVALID, checks)

    done = {t["id"] for t in backlog if t.get("status") == "done"}
    missing_deps = [d for d in contract.get("dependencies") or []
                    if d not in done]
    if missing_deps:
        check("dependencies_complete", False,
             "waiting on: %s" % ", ".join(missing_deps))
        return _verdict(RESULT_DEPENDENCY_WAIT, checks)
    check("dependencies_complete", True, "all dependencies done")

    missing_inputs = [p for p in contract.get("required_inputs") or []
                      if not os.path.exists(os.path.join(worktree, p))]
    if missing_inputs:
        check("required_inputs_exist", False,
             "missing: %s" % ", ".join(missing_inputs))
        return _verdict(RESULT_DEPENDENCY_WAIT, checks)
    check("required_inputs_exist", True, "present")

    contract_problems = bootstrap_gate.validate_task_contract(
        task, contract.get("allowed_paths") or [])
    if not check("required_outputs_coverable", not contract_problems,
                "; ".join(contract_problems) or "all coverable"):
        return _verdict(RESULT_PLATFORM_INVALID, checks)

    contradictions = _find_contradictions(contract)
    if not check("no_internal_contradiction", not contradictions,
                "; ".join(contradictions) or "none found"):
        return _verdict(RESULT_PLATFORM_INVALID, checks)

    # canonical-contract-divergence guard (item 3): the conductor's own
    # work-order expected_outputs and every acceptance criterion must
    # already be represented on the ONE compiled contract -- never
    # silently reconciled downstream by the bootstrap validator/worker
    # prompt each re-deriving their own view of "what's required".
    divergences = contract_mod.find_work_order_divergences(contract)
    if not check("work_order_matches_compiled_contract", not divergences,
                "; ".join(divergences) or "no divergence"):
        return _verdict(RESULT_PLATFORM_INVALID, checks)

    if not check("worktree_writable", os.access(worktree, os.W_OK),
                worktree):
        return _verdict(RESULT_PLATFORM_INVALID, checks)

    real_worktree = os.path.realpath(worktree)
    real_root = os.path.realpath(project_root)
    belongs = real_worktree == real_root or \
        real_worktree.startswith(real_root + os.sep)
    if not check("worktree_belongs_to_project", belongs,
                "%s not under project root %s" % (worktree, project_root)):
        return _verdict(RESULT_PLATFORM_INVALID, checks)

    if backend is not None and inventory is not None:
        verification = ((inventory.get("observed") or {})
                        .get("cli_backend_verification") or {})
        record = verification.get(backend)
        if isinstance(record, dict) and record.get("ok") is False:
            check("backend_credentials_verified", False,
                 "%s: %s" % (backend, record.get("detail")))
            return _verdict(RESULT_CREDENTIAL_REQUIRED, checks)
    check("backend_credentials_verified", True,
         backend or "not checked (no inventory)")

    if capacity_decision is not None:
        cdec = capacity_decision.get("decision")
        if cdec == "human_required":
            check("capacity_available", False,
                 capacity_decision.get("reason"))
            return _verdict(RESULT_HUMAN_REQUIRED, checks)
        if cdec == "wait":
            check("capacity_available", False,
                 capacity_decision.get("reason"))
            return _verdict(RESULT_DEPENDENCY_WAIT, checks)
        check("capacity_available", True, cdec)

    # Outstanding decisions_needed are PROJECT-level, not task-scoped: a
    # decision unrelated to THIS task must never preflight-block it --
    # that would contradict the platform's own design (the project
    # keeps making autonomous progress on whatever it CAN do while a
    # human decision remains pending elsewhere). The top-level cycle
    # gate (`_run_cycle_locked`'s human-blocker check, reached only when
    # NO task is eligible at all) is what actually pauses the project
    # for a genuine human decision -- this only catches the ONE thing
    # that legitimately IS this task's problem: a reversible decision
    # that should already have been auto-resolved at cycle start but
    # wasn't (a real platform bug worth surfacing, not a human's to
    # answer).
    unresolved_reversible = [
        text for text in (decisions_needed or [])
        if decision_policy.classify_decision(text)["category"] ==
        decision_policy.CATEGORY_REVERSIBLE]
    if unresolved_reversible:
        check("reversible_decisions_resolved", False,
             "; ".join(unresolved_reversible))
        return _verdict(
            RESULT_AUTO_REPAIRED, checks,
            note="reversible decisions should already have been "
                 "auto-resolved at cycle start; re-run "
                 "decision_policy.auto_resolve_reversible_decisions")
    check("reversible_decisions_resolved", True, "none outstanding")
    check("no_blocking_human_decisions_for_this_task", True,
         "%d project-level decision(s) outstanding, none of which gate "
         "this specific task" % len(decisions_needed or []))

    return _verdict(RESULT_FEASIBLE, checks)
