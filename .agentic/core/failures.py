"""Stable failure taxonomy (Phase 1.E).

Twelve classes, each with a documented retry/repair/fallback/cooling/
blocker/escalation policy. The critical invariant: a PLATFORM class
(the platform's own contract/capability/policy layer rejected something,
not the model or the provider) must never be recorded as if it were a
model/provider failure -- it must not increment provider failure
history, open a circuit breaker, trigger provider fallback, or trigger
ordinary (streak-escalating) cooling. See `scheduler.cooldown_breakdown`
for the distinct "platform_failure" cooling outcome this maps to."""

MODEL_OUTPUT_INVALID = "model_output_invalid"
PROVIDER_UNAVAILABLE = "provider_unavailable"
PROVIDER_AUTHENTICATION = "provider_authentication"
PROVIDER_CAPACITY = "provider_capacity"
DETERMINISTIC_CHECK_FAILED = "deterministic_check_failed"
PROJECT_DEPENDENCY_MISSING = "project_dependency_missing"
TASK_CONTRACT_INVALID = "task_contract_invalid"
PLATFORM_CAPABILITY_MISSING = "platform_capability_missing"
WORKSPACE_POLICY_DENIED = "workspace_policy_denied"
GENUINE_HUMAN_DECISION = "genuine_human_decision"
EXECUTION_TIMEOUT = "execution_timeout"
INFRASTRUCTURE_FAILURE = "infrastructure_failure"

# Classes attributable to the PLATFORM, never the model or the provider.
# `fail()` in project.py routes these to the "platform_failure" cooling
# outcome (see scheduler.py) instead of escalating the ordinary
# exponential failure-streak backoff, and they never touch the provider
# circuit breaker or trust ledger (neither of which project.py's cycle
# path calls for a failure that never reached a real backend invocation
# -- see project.py's preflight/contract-validation short-circuits).
PLATFORM_CLASSES = frozenset({
    PROJECT_DEPENDENCY_MISSING, TASK_CONTRACT_INVALID,
    PLATFORM_CAPABILITY_MISSING, WORKSPACE_POLICY_DENIED,
})

RETRY_IMMEDIATE = "retry_immediate"
RETRY_AFTER_REPAIR = "retry_after_repair"
RETRY_NEXT_CYCLE = "retry_next_cycle"
RETRY_NEVER_AUTOMATIC = "retry_never_automatic"

TAXONOMY = {
    MODEL_OUTPUT_INVALID: {
        "retry_policy": RETRY_AFTER_REPAIR,
        "repair_policy": "structured repair packet with failing evidence",
        "fallback_eligible": True,
        "cooling_behaviour": "ordinary_failure_cooldown",
        "blocker_behaviour": "block_after_repair_attempts_exhausted",
        "human_escalation": "after_repeated_disagreement",
        "persist_evidence": True,
    },
    PROVIDER_UNAVAILABLE: {
        "retry_policy": RETRY_NEXT_CYCLE,
        "repair_policy": "none (not a content problem)",
        "fallback_eligible": True,
        "cooling_behaviour": "breaker_recovery_estimate",
        "blocker_behaviour": "no_blocker_handoff_to_fallback",
        "human_escalation": "if_no_backend_remains",
        "persist_evidence": True,
    },
    PROVIDER_AUTHENTICATION: {
        "retry_policy": RETRY_NEVER_AUTOMATIC,
        "repair_policy": "none (credential problem)",
        "fallback_eligible": True,
        "cooling_behaviour": "breaker_authentication_required",
        "blocker_behaviour": "no_blocker_handoff_to_fallback",
        "human_escalation": "always",
        "persist_evidence": True,
    },
    PROVIDER_CAPACITY: {
        "retry_policy": RETRY_NEXT_CYCLE,
        "repair_policy": "none",
        "fallback_eligible": True,
        "cooling_behaviour": "provider_retry_after_or_capacity_default",
        "blocker_behaviour": "no_blocker_handoff_to_fallback",
        "human_escalation": "if_no_backend_remains",
        "persist_evidence": True,
    },
    DETERMINISTIC_CHECK_FAILED: {
        "retry_policy": RETRY_AFTER_REPAIR,
        "repair_policy": "failing check names + detail fed back to worker",
        "fallback_eligible": False,
        "cooling_behaviour": "ordinary_failure_cooldown",
        "blocker_behaviour": "block_after_repair_attempts_exhausted",
        "human_escalation": "after_repair_attempts_exhausted",
        "persist_evidence": True,
    },
    PROJECT_DEPENDENCY_MISSING: {
        "retry_policy": RETRY_NEXT_CYCLE,
        "repair_policy": "none (waits on another task/resource)",
        "fallback_eligible": False,
        "cooling_behaviour": "platform_failure_cooldown",
        "blocker_behaviour": "pending_not_blocked",
        "human_escalation": "never_automatic",
        "persist_evidence": True,
    },
    TASK_CONTRACT_INVALID: {
        "retry_policy": RETRY_AFTER_REPAIR,
        "repair_policy": "replan by conductor/architect",
        "fallback_eligible": False,
        "cooling_behaviour": "platform_failure_cooldown",
        "blocker_behaviour": "block_with_platform_blocker",
        "human_escalation": "if_replan_repeats",
        "persist_evidence": True,
    },
    PLATFORM_CAPABILITY_MISSING: {
        "retry_policy": RETRY_NEXT_CYCLE,
        "repair_policy": "recovered automatically once the platform gap "
                         "is fixed (see bootstrap_gate recovery)",
        "fallback_eligible": False,
        "cooling_behaviour": "platform_failure_cooldown",
        "blocker_behaviour": "block_with_platform_blocker",
        "human_escalation": "if_no_platform_fix_available",
        "persist_evidence": True,
    },
    WORKSPACE_POLICY_DENIED: {
        "retry_policy": RETRY_AFTER_REPAIR,
        "repair_policy": "conductor must narrow/correct allowed_paths",
        "fallback_eligible": False,
        "cooling_behaviour": "platform_failure_cooldown",
        "blocker_behaviour": "block_with_platform_blocker",
        "human_escalation": "if_repeats",
        "persist_evidence": True,
    },
    GENUINE_HUMAN_DECISION: {
        "retry_policy": RETRY_NEVER_AUTOMATIC,
        "repair_policy": "none -- a human must decide",
        "fallback_eligible": False,
        "cooling_behaviour": "no_cooling_project_paused_for_human",
        "blocker_behaviour": "human_only_blocker",
        "human_escalation": "always",
        "persist_evidence": True,
    },
    EXECUTION_TIMEOUT: {
        "retry_policy": RETRY_NEXT_CYCLE,
        "repair_policy": "narrower scope on retry",
        "fallback_eligible": True,
        "cooling_behaviour": "ordinary_failure_cooldown",
        "blocker_behaviour": "block_after_repair_attempts_exhausted",
        "human_escalation": "after_repeated_timeout",
        "persist_evidence": True,
    },
    INFRASTRUCTURE_FAILURE: {
        "retry_policy": RETRY_NEXT_CYCLE,
        "repair_policy": "none (git/filesystem/OS-level failure)",
        "fallback_eligible": False,
        "cooling_behaviour": "ordinary_failure_cooldown",
        "blocker_behaviour": "block_worktree_preserved_as_evidence",
        "human_escalation": "if_repeats",
        "persist_evidence": True,
    },
}


def is_platform_class(failure_class):
    return failure_class in PLATFORM_CLASSES


def cooling_outcome_for(failure_class, outcome):
    """The `scheduler.start_cooling`/`cooldown_breakdown` outcome string
    to use for a given failure class -- `None` means "use the ordinary
    outcome-based cooling unchanged" (the default for every class not
    listed here, and for `failure_class=None`)."""
    if failure_class == GENUINE_HUMAN_DECISION:
        return None   # the project pauses via the human-blocker gate,
                      # not via scheduler cooling at all
    if is_platform_class(failure_class):
        return "platform_failure"
    return None


def policy_for(failure_class):
    return TAXONOMY.get(failure_class)
