"""Hierarchical Orchestration (Phase 9): role-aware, model-CLASS-aware
backend/model selection on top of the Model Capability Registry
(core.modelcap, Phase 8) and the Canonical Role Registry (core.rolereg).

Every decision is explainable -- returned as a structured record with a
step-by-step `explanation` list, never just a bare backend name -- and
frontier-class capacity is reserved for the roles that actually need it
(architecture approval, Capability Plan approval, milestone approval,
final audit, repeated repair arbitration): an ordinary worker task never
silently consumes it once the configured reserve is exhausted.

This module does NOT replace `core.routing.capability_chain` or
`core.backends.routing_chain` -- both keep routing by backend NAME
exactly as they do today. This is an additive layer for callers that
want a role's hierarchy tier honoured; Phase 10 wires it into the live
cycle loop.
"""
from . import rolereg

DEFAULT_ORCHESTRATION_CONFIG = {
    "orchestrator": {"required_class": "frontier", "fallback_class": "high",
                     "reserve_capacity_percent": 25},
    "workers": {"default_class": "medium", "high_risk_class": "high"},
    "reviewers": {"prefer_different_provider": True,
                 "minimum_class": "high"},
    "lightweight": {"tasks": list(rolereg.DEFAULT_LIGHTWEIGHT_TASKS)},
}


def orchestration_config(cfg):
    merged = {k: dict(v) for k, v in DEFAULT_ORCHESTRATION_CONFIG.items()}
    for key, overrides in (cfg.get("orchestration") or {}).items():
        merged.setdefault(key, {})
        if isinstance(overrides, dict):
            merged[key].update(overrides)
        else:
            merged[key] = overrides
    return merged


# -- frontier capacity reservation --------------------------------------------------

def frontier_calls_by_reservation(ledger, model_registry, *, window_hours=24):
    """{"reserved": N, "worker": N, "total": N} -- calls made against any
    CURRENTLY frontier-class backend in the window, split by whether the
    role that made the call reserves frontier capacity. Reuses
    `CapacityLedger.calls_in_window` (already role-tagged) -- never a
    second usage ledger."""
    frontier_backends = {r["backend"] for r in model_registry.records
                         if r["reasoning_class"] == "frontier"
                         and r["available"]}
    reserved, worker = 0, 0
    for backend in frontier_backends:
        for row in ledger.calls_in_window(backend, window_hours):
            spec = rolereg.role_spec(row.get("role"))
            if spec and spec.get("reserves_frontier_capacity"):
                reserved += 1
            else:
                worker += 1
    return {"reserved": reserved, "worker": worker,
           "total": reserved + worker}


def frontier_capacity_status(cfg, ledger, model_registry, *,
                             window_hours=24):
    """('ok' | 'reserve_exhausted', detail). Only meaningful for a
    caller about to spend frontier capacity on NON-reserving work --
    a reserving role (architect, final_auditor, ...) is never blocked by
    its own reservation."""
    orch_cfg = orchestration_config(cfg)["orchestrator"]
    reserve_percent = orch_cfg.get("reserve_capacity_percent", 25)
    counts = frontier_calls_by_reservation(ledger, model_registry,
                                           window_hours=window_hours)
    if counts["total"] == 0:
        return "ok", dict(counts, reserve_percent=reserve_percent,
                          worker_share=0.0)
    worker_share = round(counts["worker"] / counts["total"], 3)
    ceiling = (100 - reserve_percent) / 100.0
    status = "reserve_exhausted" if worker_share >= ceiling else "ok"
    return status, dict(counts, reserve_percent=reserve_percent,
                        worker_share=worker_share)


# -- role-aware, explainable selection -----------------------------------------------

def select_for_role(role, cfg, model_registry, ledger=None, *,
                    task_risk="medium", task_kind=None,
                    worker_backend=None):
    """The Phase 9 entry point. Returns an explainable RoutingDecision:
    {"role", "ok", "backend", "model_id", "class", "reason",
     "explanation": [...], "candidate": ModelRecord-or-None}.

    Never mutates state. `worker_backend`: the backend a reviewer's
    corresponding worker used, so reviewer independence can actually
    prefer a different provider (not merely a config flag)."""
    explanation = []
    spec = rolereg.role_spec(role)
    if spec is None:
        return {"role": role, "ok": False, "backend": None,
               "model_id": None, "class": None,
               "reason": "role %r is not in the canonical role registry"
               % role, "explanation": [], "candidate": None}

    orch_cfg = orchestration_config(cfg)
    tier = spec["tier"]
    required_class = spec["required_class"]
    fallback_class = spec["fallback_class"]

    if task_kind and rolereg.is_lightweight_task(task_kind, cfg):
        required_class = "lightweight"
        explanation.append("task_kind %r is always routed lightweight "
                           "per orchestration.lightweight.tasks"
                           % task_kind)
    elif tier == "worker":
        if task_risk == "high":
            required_class = orch_cfg["workers"].get(
                "high_risk_class", spec.get("high_risk_class",
                                            required_class))
            explanation.append("high-risk task -> workers.high_risk_class "
                               "%r" % required_class)
        else:
            required_class = orch_cfg["workers"].get("default_class",
                                                      required_class)
            explanation.append("worker role -> workers.default_class %r"
                               % required_class)
    elif tier == "reviewer":
        required_class = orch_cfg["reviewers"].get("minimum_class",
                                                    required_class)
        explanation.append("reviewer role -> reviewers.minimum_class %r"
                           % required_class)
    elif tier == "orchestrator":
        required_class = orch_cfg["orchestrator"].get("required_class",
                                                       required_class)
        fallback_class = orch_cfg["orchestrator"].get("fallback_class",
                                                       fallback_class)
        explanation.append("orchestrator-tier role -> required_class %r "
                           "(fallback %r)" % (required_class,
                                              fallback_class))

    if required_class == "frontier" and \
            not spec["reserves_frontier_capacity"] and ledger is not None:
        status, detail = frontier_capacity_status(cfg, ledger,
                                                   model_registry)
        if status == "reserve_exhausted":
            # demote exactly one tier (never jump straight to the role's
            # worst-case fallback_class) -- best()'s own downward
            # degradation still finds whatever's actually available at
            # or below "high"; the role's fallback_class remains the
            # final safety net below, only used if even that finds nothing
            required_class = "high"
            explanation.append(
                "frontier capacity reserve exhausted (worker_share=%.0f%% "
                ">= reserve ceiling) -- demoting to %r to protect "
                "reserved roles rather than spend the reserve on "
                "ordinary work" % (detail["worker_share"] * 100,
                                   required_class))

    record = model_registry.best(required_class)
    if record is None and required_class != fallback_class:
        explanation.append("no available model at class %r; trying "
                           "fallback %r" % (required_class, fallback_class))
        record = model_registry.best(fallback_class)

    if record is not None and tier == "reviewer" and \
            orch_cfg["reviewers"].get("prefer_different_provider") and \
            worker_backend and record["backend"] == worker_backend:
        alt = [r for r in model_registry.by_class(record["reasoning_class"])
              if r["backend"] != worker_backend]
        if alt:
            explanation.append(
                "reviewer independence: preferring a different provider "
                "than the worker (%r) -> %r" % (worker_backend,
                                                 alt[0]["backend"]))
            record = alt[0]
        else:
            explanation.append(
                "reviewer independence requested but no alternative "
                "provider is currently available; proceeding with %r "
                "(recorded, never silently substituted for something "
                "worse)" % worker_backend)

    if record is None:
        explanation.append("no backend/model available at class %r or "
                           "fallback %r" % (required_class, fallback_class))
        return {"role": role, "ok": False, "backend": None,
               "model_id": None, "class": None,
               "reason": explanation[-1], "explanation": explanation,
               "candidate": None}

    explanation.append("selected backend=%r model=%r (class=%r)"
                       % (record["backend"], record["model_id"],
                          record["reasoning_class"]))
    return {"role": role, "ok": True, "backend": record["backend"],
           "model_id": record["model_id"],
           "class": record["reasoning_class"], "reason": explanation[-1],
           "explanation": explanation, "candidate": record}
