"""Canonical Role Registry (Phase 9): the single source of truth for
every agent role this platform uses, replacing the three independently
maintained role lists Phase 0 found drifting apart (`routing.py`'s
`ROLE_ALIASES`/`REVIEWER_ROLES`, `setupwiz.py`'s hardcoded role tuple,
and inline role-string literals scattered through `project.py`).

This module ADDS hierarchy metadata (which model-capability tier a role
needs, whether it reserves frontier capacity, whether it may write code,
whether independent-provider review is expected) on top of
`core.routing`'s existing alias/reviewer tables -- it does not replace
or rename them, so every existing call site that passes a role string
keeps working unchanged.
"""
from .routing import REVIEWER_ROLES as _ROUTING_REVIEWER_ROLES
from .routing import ROLE_ALIASES

# The ten hierarchical roles from the design spec, mapped onto the
# platform's actual role strings so nothing already in use needs to
# change. `frontier_orchestrator` is the standalone top-of-hierarchy
# role (Phase 10 wires it in for milestone/architecture approval);
# `architect`/`final_auditor` are its two role-specific instances that
# already exist in `project.py` today.
ROLES = {
    "frontier_orchestrator": {
        "tier": "orchestrator", "required_class": "frontier",
        "fallback_class": "high", "reserves_frontier_capacity": True,
        "may_write_code": False,
        "description": "Understands complete project intent; approves "
                       "architecture and the Capability Plan; resolves "
                       "major ambiguity and agent disagreements; "
                       "approves milestones; supervises the final audit.",
    },
    "architect": {
        "tier": "orchestrator", "required_class": "frontier",
        "fallback_class": "high", "reserves_frontier_capacity": True,
        "may_write_code": False,
        "description": "Project Architect: produces the initial "
                       "architecture from the plan/Capability Plan.",
    },
    "conductor": {
        "tier": "conductor", "required_class": "high",
        "fallback_class": "medium", "reserves_frontier_capacity": False,
        "may_write_code": False,
        "description": "Turns one backlog task into a bounded work "
                       "order for a worker.",
    },
    "capability_curator": {
        "tier": "curator", "required_class": "medium",
        "fallback_class": "lightweight", "reserves_frontier_capacity": False,
        "may_write_code": False,
        "description": "Searches/compares/recommends capabilities and "
                       "skills; structurally cannot approve or install "
                       "anything (core.skillmarket.SkillCurator).",
    },
    "coder": {
        "tier": "worker", "required_class": "medium",
        "fallback_class": "lightweight", "reserves_frontier_capacity": False,
        "may_write_code": True, "high_risk_class": "high",
        "description": "Specialist Worker: implements one work order.",
    },
    "ui_designer": {
        "tier": "worker", "required_class": "medium",
        "fallback_class": "lightweight", "reserves_frontier_capacity": False,
        "may_write_code": True, "high_risk_class": "high",
        "description": "Specialist Worker for UI-shaped tasks.",
    },
    "qa": {
        "tier": "reviewer", "required_class": "high",
        "fallback_class": "medium", "reserves_frontier_capacity": False,
        "may_write_code": False, "prefer_different_provider": True,
        "description": "QA Reviewer: independent verification against "
                       "done_when/acceptance criteria.",
    },
    "security": {
        "tier": "reviewer", "required_class": "high",
        "fallback_class": "medium", "reserves_frontier_capacity": False,
        "may_write_code": False, "prefer_different_provider": True,
        "description": "Security Reviewer.",
    },
    "accessibility_reviewer": {
        "tier": "reviewer", "required_class": "high",
        "fallback_class": "medium", "reserves_frontier_capacity": False,
        "may_write_code": False, "prefer_different_provider": True,
        "description": "Accessibility Reviewer.",
    },
    "seo_reviewer": {
        "tier": "reviewer", "required_class": "medium",
        "fallback_class": "lightweight", "reserves_frontier_capacity": False,
        "may_write_code": False, "prefer_different_provider": False,
        "description": "SEO Reviewer.",
    },
    "final_auditor": {
        "tier": "orchestrator", "required_class": "frontier",
        "fallback_class": "high", "reserves_frontier_capacity": True,
        "may_write_code": False,
        "description": "Final Auditor: evidence-based completion audit.",
    },
    # legacy repository-maintenance (tick-mode) roles -- same policy
    "triage": {
        "tier": "conductor", "required_class": "lightweight",
        "fallback_class": "lightweight", "reserves_frontier_capacity": False,
        "may_write_code": False,
        "description": "Legacy tick-mode triage classification.",
    },
    "verifier": {
        "tier": "reviewer", "required_class": "high",
        "fallback_class": "medium", "reserves_frontier_capacity": False,
        "may_write_code": False, "prefer_different_provider": True,
        "description": "Legacy tick-mode independent verifier.",
    },
    "memory_summarizer": {
        "tier": "curator", "required_class": "lightweight",
        "fallback_class": "lightweight", "reserves_frontier_capacity": False,
        "may_write_code": False,
        "description": "Summarises memory/context -- a lightweight task "
                       "per the example orchestration policy.",
    },
}

WORKER_ROLES = frozenset(name for name, spec in ROLES.items()
                         if spec["tier"] == "worker")
# this module's reviewer-tier roles, plus routing.py's existing set --
# a role can need independent-provider review (routing.py's concern) and
# be frontier/orchestrator-tier at the same time (e.g. final_auditor);
# the two mechanisms compose rather than conflict.
REVIEWER_ROLES = frozenset(
    name for name, spec in ROLES.items() if spec["tier"] == "reviewer") \
    | _ROUTING_REVIEWER_ROLES
ORCHESTRATOR_ROLES = frozenset(name for name, spec in ROLES.items()
                               if spec["tier"] == "orchestrator")
FRONTIER_RESERVING_ROLES = frozenset(
    name for name, spec in ROLES.items()
    if spec["reserves_frontier_capacity"])

# Task kinds the example orchestration policy always routes to the
# cheapest ("lightweight") class regardless of the calling role's tier.
DEFAULT_LIGHTWEIGHT_TASKS = ("classification", "registry_search",
                             "log_summary", "formatting")


def canonical_role(role):
    """Resolve an alias (routing.py's existing table) to its canonical
    name; unknown roles pass through unchanged (never invented)."""
    return ROLE_ALIASES.get(role, role)


def role_spec(role):
    """Role metadata, or None for a role this registry doesn't know --
    callers decide how to handle an unknown role; this module never
    guesses one."""
    return ROLES.get(canonical_role(role))


def is_lightweight_task(task_kind, cfg=None):
    tasks = DEFAULT_LIGHTWEIGHT_TASKS
    if cfg is not None:
        tasks = ((cfg.get("orchestration") or {}).get("lightweight") or {}) \
            .get("tasks", DEFAULT_LIGHTWEIGHT_TASKS)
    return task_kind in tasks


def authorise_frontier_coding(role, *, no_suitable_worker=False,
                              exceptionally_critical=False,
                              repair_escalation=False):
    """Code-enforced version of "the orchestrator should not perform
    ordinary coding unless...". Returns (authorised: bool, reason: str).
    A caller that ignores a `False` result and dispatches anyway is a
    bug in the caller, not something this function can prevent -- but
    nothing here ever itself returns True without one of the three
    named exceptions holding."""
    spec = role_spec(role)
    if spec is None or spec["tier"] != "orchestrator":
        return True, "role %r is not an orchestrator-tier role; the " \
            "restriction does not apply" % role
    if no_suitable_worker:
        return True, "no suitable worker exists for this task"
    if exceptionally_critical:
        return True, "task is exceptionally critical"
    if repair_escalation:
        return True, "repair escalation requires frontier reasoning"
    return False, "orchestrator-tier role %r may not perform ordinary " \
        "coding without one of: no suitable worker, exceptional " \
        "criticality, or repair escalation" % role
