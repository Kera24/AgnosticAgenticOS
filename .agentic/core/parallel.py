"""Phase 5 -- Supervised Parallelism.

Parallelism is risk-based and capacity-aware. Most tasks run through the
existing single-agent path in `project.py` completely unchanged --
`decide_agent_count` returns 1 for them and nothing here is invoked at
all. Only a task that trips one of the six documented risk signals fans
out into multiple isolated candidates: each on its own git worktree and
branch, given the IDENTICAL immutable work order, an explicit
backend/model, and no shared mutable state -- so one candidate's crash
or bad diff can never touch another. A winner is chosen from
deterministic evidence only (QA verdict, acceptance-criteria coverage
from the deterministic gate, change footprint as a maintainability
tie-break); a failed deterministic gate always disqualifies a candidate
regardless of any other signal. Security review and integration
(merging one branch into agentic/project) stay exactly the single,
already-tested step they always were in `project.py` -- applied once,
to the winner's worktree, after selection. Losing candidates are never
merged and never deleted; their worktrees/branches are preserved as
evidence, same retention policy as any other failed task worktree.
"""
import os

from . import failures
from . import gate as gate_mod
from . import gitops

DEFAULTS = {"max_projects_global": 4, "max_agents_global": 3,
           "default_agents_per_task": 1}

RISK_SIGNAL_ARCHITECTURE = "difficult_architecture"
RISK_SIGNAL_HIGH_RISK_REFACTOR = "high_risk_refactoring"
RISK_SIGNAL_HARD_DEBUGGING = "hard_debugging"
RISK_SIGNAL_UI_PROPOSALS = "competing_ui_proposals"
RISK_SIGNAL_SECURITY = "security_sensitive"
RISK_SIGNAL_REPEATED_FAILURE = "repeated_single_agent_failure"

UI_PATH_HINTS = ("ui/", "frontend/", "components/", "styles/", ".css",
                 ".scss", ".tsx", ".jsx", ".vue", ".svelte")

REPEATED_FAILURE_THRESHOLD = 2   # this task has already failed >=1 time


def _cfg(cfg):
    merged = dict(DEFAULTS)
    merged.update(cfg.get("parallelism") or {})
    return merged


def detect_risk_signals(task):
    """Which of the six documented triggers this task actually hits.
    Every signal here is read directly off the task record -- never
    inferred from free text or guessed -- so this function is exactly
    as trustworthy as the architect/conductor's own classification of
    the task."""
    signals = []
    risk = (task.get("risk") or "low").lower()
    if risk == "high":
        signals.append(RISK_SIGNAL_ARCHITECTURE)
        signals.append(RISK_SIGNAL_HIGH_RISK_REFACTOR)
    if task.get("security_relevant"):
        signals.append(RISK_SIGNAL_SECURITY)
    if int(task.get("attempts") or 0) >= REPEATED_FAILURE_THRESHOLD:
        signals.append(RISK_SIGNAL_REPEATED_FAILURE)
    if risk in ("high", "medium"):
        expected = task.get("expected_paths") or []
        paths = [p if isinstance(p, str) else (p or {}).get("path", "")
                for p in expected]
        if any(any(hint in path for hint in UI_PATH_HINTS)
              for path in paths):
            signals.append(RISK_SIGNAL_UI_PROPOSALS)
    if risk == "high" and int(task.get("attempts") or 0) >= 1:
        signals.append(RISK_SIGNAL_HARD_DEBUGGING)
    return signals


def decide_agent_count(task, cfg):
    """(agent_count, signals). 1 (native single-agent, unchanged) unless
    a real risk signal is present; capped by parallelism.max_agents_global.
    Two comparable candidates are enough to get genuine selection evidence
    without unbounded fan-out cost, so a trigger always asks for exactly
    2, never more, regardless of how many of the six signals fired."""
    p = _cfg(cfg)
    default_n = max(1, int(p.get("default_agents_per_task", 1)))
    cap = max(1, int(p.get("max_agents_global", 3)))
    signals = detect_risk_signals(task)
    if not signals:
        return default_n, signals
    return min(2, cap), signals


_BACKEND_ERROR_KIND_TO_CLASS = {
    "rate_limit": (failures.PROVIDER_CAPACITY, "candidate_provider_capacity"),
    "usage_limit": (failures.PROVIDER_CAPACITY, "candidate_provider_capacity"),
    "authentication": (failures.PROVIDER_AUTHENTICATION,
                       "candidate_provider_authentication"),
    "timeout": (failures.EXECUTION_TIMEOUT, "candidate_execution_timeout"),
    "provider_unavailable": (failures.PROVIDER_UNAVAILABLE,
                            "candidate_provider_unavailable"),
    "connection_error": (failures.PROVIDER_UNAVAILABLE,
                         "candidate_provider_unavailable"),
    "network_error": (failures.PROVIDER_UNAVAILABLE,
                      "candidate_provider_unavailable"),
}


def _classify_disqualification(stage, detail=None):
    """Structured cause for a disqualified candidate (item 1 of the
    aggregate-blocker fix): every disqualification records
    (failure_class, code, platform_owned, retryable) instead of only a
    free-text reason, so an aggregate outcome can be classified from its
    constituent causes -- never by re-parsing a concatenated string (see
    `run_candidates`'s `candidate_failures` and `core.parallel_recovery`).
    Nothing here is ever platform_owned=True: a genuinely platform-owned
    candidate cause would require identifying a specific, already-fixed
    platform defect, which -- for NEWLY observed disqualifications -- is
    never inferred from free text (only legacy-blocker MIGRATION does
    that; see core.parallel_recovery.reconstruct_legacy_candidate_failures)."""
    if stage == "engine_error":
        return failures.INFRASTRUCTURE_FAILURE, "candidate_engine_error", \
            False, True
    if stage == "coder_failed":
        return _BACKEND_ERROR_KIND_TO_CLASS.get(
            detail, (failures.MODEL_OUTPUT_INVALID, "candidate_coder_failed"))\
            + (False, True)
    if stage == "coder_blocked":
        return failures.MODEL_OUTPUT_INVALID, "candidate_coder_blocked", \
            False, True
    if stage == "scope_violation":
        return failures.MODEL_OUTPUT_INVALID, "candidate_scope_violation", \
            False, True
    if stage == "gate_no_checks":
        return failures.DETERMINISTIC_CHECK_FAILED, \
            "candidate_no_deterministic_checks", False, True
    if stage == "gate_failed":
        return failures.DETERMINISTIC_CHECK_FAILED, \
            "candidate_deterministic_check_failed", False, True
    return failures.MODEL_OUTPUT_INVALID, "candidate_disqualified", False, True


def _disqualify(record, stage, reason, detail=None, evidence_ref=None):
    record["disqualified"] = True
    record["disqualify_reason"] = reason
    failure_class, code, platform_owned, retryable = \
        _classify_disqualification(stage, detail)
    record["failure_class"] = failure_class
    record["code"] = code
    record["platform_owned"] = platform_owned
    record["retryable"] = retryable
    record["evidence_ref"] = evidence_ref
    return record


def _run_one_candidate(cfg, caller, engine, task, order, worker_role,
                       worktree, candidate_id, chain, run_id, run_dir,
                       protected, authorised_exceptions, log, load_prompt,
                       schema, review_input_fn, apply_and_check_fn):
    from . import execengine
    record = {"candidate_id": candidate_id, "worktree": worktree,
             "backend": chain[0] if chain else None, "disqualified": False,
             "disqualify_reason": None, "gate_result": None, "qa_out": None,
             "qa_verdict": "uncertain", "changed_files": [],
             "lines_changed": 0, "acceptance_coverage": (0, 0),
             "failure_class": None, "code": None, "platform_owned": False,
             "retryable": True, "evidence_ref": None}
    try:
        session = engine.launch_agent(execengine.SessionRequest(
            run_id=run_id, task_id=candidate_id, worktree=worktree,
            role=worker_role, coder_input={"work_order": order},
            chain=chain))
        result = session.to_legacy_dict()
    except Exception as exc:   # noqa: BLE001 -- one candidate's crash must
                               # never take the others (or the cycle) down
        log({"event": "parallel_candidate_error", "run_id": run_id,
             "candidate_id": candidate_id, "detail": str(exc)[:300]})
        return _disqualify(record, "engine_error", "engine error: %s" % exc)
    if not result.get("ok"):
        kind = (result.get("error") or {}).get("kind", "?")
        return _disqualify(record, "coder_failed",
                           "coder failed: %s" % kind, detail=kind)
    record["backend"] = result.get("backend", record["backend"])
    if result.get("blocked"):
        return _disqualify(record, "coder_blocked",
                           result.get("blocker") or "coder blocked")
    violations = apply_and_check_fn(cfg, result, order, worktree, protected,
                                    authorised_exceptions, log=log)
    if violations:
        return _disqualify(record, "scope_violation",
                           "scope violations: %s" % "; ".join(violations[:3]),
                           evidence_ref=worktree)
    checks_dir = os.path.join(run_dir, "checks-%s" % candidate_id)
    gate_result = gate_mod.run_checks(cfg, worktree, checks_dir)
    record["gate_result"] = gate_result
    # Deliberately narrower than the single-agent path here: candidates
    # never get the bootstrap/structural-gate fallback or a repair loop
    # (see the Phase 5 delivery report) -- a candidate with zero
    # deterministic checks, or one that fails them, is simply
    # disqualified so the OTHER candidate (or a full task failure/block
    # if none survive) decides the outcome, rather than silently
    # reintroducing the pre-Phase-3 "no checks = pass" gap.
    if gate_result.get("no_checks") or not gate_result.get("ok"):
        no_checks = gate_result.get("no_checks")
        return _disqualify(
            record, "gate_no_checks" if no_checks else "gate_failed",
            "deterministic checks failed" if not no_checks
            else "no deterministic checks", evidence_ref=checks_dir)
    record["changed_files"] = gitops.changed_files(worktree)
    record["lines_changed"] = gitops.changed_lines(worktree)
    dw_results = gate_result.get("results") or []
    record["acceptance_coverage"] = (
        sum(1 for r in dw_results if r.get("passed")), len(dw_results))
    qa_input = review_input_fn(order, worktree, gate_result, task)
    qa = caller("qa", load_prompt("qa-review.md", shared=False), qa_input,
               schema=schema("verification.schema.json"),
               workspace=worktree, permissions="read")
    qa_out = qa["structured_output"] if qa.get("ok") else None
    record["qa_out"] = qa_out
    record["qa_verdict"] = (qa_out or {}).get("verdict", "uncertain")
    return record


def _score(candidate):
    qa_pass = 1 if candidate["qa_verdict"] == "pass" else 0
    passed, total = candidate["acceptance_coverage"]
    coverage = (passed / total) if total else 0.0
    # smaller footprint is preferred as a maintainability tie-break, all
    # else equal -- never the primary signal
    return (qa_pass, coverage, -candidate["lines_changed"])


def select_winner(candidates):
    """(winner_or_None, ranked_survivors, reasoning). A failed
    deterministic gate is disqualifying no matter what else a candidate
    has going for it -- the supervisor/reviewer voice (QA verdict here)
    is only ever a ranking signal AMONG candidates that already passed
    deterministic checks, never an override of one that didn't."""
    survivors = [c for c in candidates if not c["disqualified"]]
    if not survivors:
        detail = "; ".join("%s: %s" % (c["candidate_id"],
                                       c["disqualify_reason"])
                           for c in candidates)
        return None, [], ("all %d candidate(s) disqualified (%s)"
                          % (len(candidates), detail[:300]))
    ranked = sorted(survivors, key=_score, reverse=True)
    winner = ranked[0]
    reasoning = ("winner=%s (qa=%s, acceptance=%d/%d, lines_changed=%d) "
                "among %d surviving candidate(s) of %d"
                % (winner["candidate_id"], winner["qa_verdict"],
                   winner["acceptance_coverage"][0],
                   winner["acceptance_coverage"][1],
                   winner["lines_changed"], len(survivors), len(candidates)))
    return winner, ranked, reasoning


def decide_integration(winner, ranked):
    """(integration_allowed, reason). Auto-integration requires an
    UNAMBIGUOUS winner -- a tie on every ranking signal against the
    runner-up is never broken by guessing; the platform preserves both
    candidates and asks a human instead. (Security policy and
    "all checks pass" are enforced by the existing, unchanged
    security-review/gate steps that run downstream of this decision on
    the winner's own worktree -- not duplicated here.)"""
    if winner is None:
        return False, "no surviving candidate"
    if len(ranked) > 1 and _score(winner) == _score(ranked[1]):
        return False, ("tie between %s and %s on every ranking signal; "
                       "ambiguous, human review required"
                       % (winner["candidate_id"], ranked[1]["candidate_id"]))
    return True, "unambiguous winner"


def run_candidates(cfg, caller, engine, task, order, worker_role, root,
                   agentic_dir, base_branch, chain, run_id, run_dir,
                   protected, authorised_exceptions, primary_worktree,
                   log, load_prompt, schema, review_input_fn,
                   apply_and_check_fn):
    """Fan out to `decide_agent_count(task, cfg)` isolated candidates
    (candidate 0 reuses the worktree/branch the caller already created
    for `task["id"]` -- no wasted worktree when nothing needs a second
    opinion), run each one to a single deterministic-gate + QA verdict
    (no repair loop -- see the Phase 5 delivery report), and select a
    winner from deterministic evidence. Returns a dict: `winner` (record
    or None), `candidates` (all records, for preservation/logging),
    `reasoning`, `integration_allowed`, `reason`."""
    from . import taskspace
    n, signals = decide_agent_count(task, cfg)
    candidates = []
    for i in range(n):
        candidate_id = task["id"] if i == 0 else "%s--c%d" % (task["id"],
                                                               i + 1)
        worktree = primary_worktree if i == 0 else \
            taskspace.create_task_worktree(root, agentic_dir, candidate_id,
                                           base_branch)
        candidate_chain = chain[i % len(chain):] + chain[:i % len(chain)] \
            if chain else chain
        record = _run_one_candidate(
            cfg, caller, engine, task, order, worker_role, worktree,
            candidate_id, candidate_chain, run_id, run_dir, protected,
            authorised_exceptions, log, load_prompt, schema,
            review_input_fn, apply_and_check_fn)
        candidates.append(record)
        log({"event": "parallel_candidate_result", "run_id": run_id,
             "task_id": task["id"], "candidate_id": candidate_id,
             "disqualified": record["disqualified"],
             "qa_verdict": record["qa_verdict"]})
    winner, ranked, reasoning = select_winner(candidates)
    integration_allowed, integration_reason = decide_integration(
        winner, ranked)
    # structured candidate_failures (item 1): every disqualified
    # candidate's cause, never a free-form concatenated string -- this is
    # what an aggregate "all candidates disqualified" outcome is
    # classified from (core.parallel_recovery.classify_aggregate), and
    # what a persisted aggregate blocker carries as evidence.
    candidate_failures = [
        {"candidate_id": c["candidate_id"],
         "failure_class": c.get("failure_class"), "code": c.get("code"),
         "platform_owned": bool(c.get("platform_owned")),
         "retryable": c.get("retryable", True),
         "evidence_ref": c.get("evidence_ref"),
         "detail": c.get("disqualify_reason")}
        for c in candidates if c["disqualified"]]
    return {"winner": winner, "candidates": candidates,
           "candidate_failures": candidate_failures,
           "signals": signals, "reasoning": reasoning,
           "integration_allowed": integration_allowed,
           "reason": integration_reason if winner is not None
           else reasoning}
