"""Full-project build orchestration.

plan -> architect -> persistent backlog -> per-cycle:
capacity decision -> conductor -> coder (isolated persistent worktree) ->
deterministic checks (+ repair loop / structured handoff) -> QA reviewer ->
conditional security reviewer -> cycle commit + state update -> cooling ->
next task or final audit -> notification.

Everything runs through the common backend interface; no agent approves its
own work; a deterministic failure is never overridden."""
import datetime as _dt
import json
import os
import re

from . import backends, bootstrap_gate, capacity as capacity_mod
from . import config as config_mod, contract as contract_mod
from . import contract_recovery
from . import decision_policy, execengine, failures, featuregates, fscap
from . import inventory as inventory_mod
from . import parallel as parallel_mod
from . import parallel_recovery
from . import preflight as preflight_mod
from . import supervisor as supervisor_mod
from . import errors, gate, gitops, logs, notify, projstate
from .breaker import BreakerBoard
from .orchestrator import (apply_edits, load_prompt, _schema, _snapshot)
from .redact import looks_like_secret_in_diff, redact
from .scheduler import Scheduler

SECURITY_PATH_TRIGGERS = [
    "**/auth/**", "**/session*", "**/login*", "**/*password*", "**/*secret*",
    "**/*credential*", "**/payment*", "**/billing/**", "**/upload*",
    "**/migrations/**", "**/*.sql", "**/crypto*", "**/deploy/**",
    "requirements*.txt", "package.json", "pyproject.toml", "go.mod",
    "Cargo.toml", ".env.example", "Dockerfile",
]
SECURITY_DIFF_RE = re.compile(
    r"(?i)(password|secret|token|api[_-]?key|execute\s*\(|subprocess|"
    r"os\.system|eval\(|pickle\.loads|verify\s*=\s*False|md5|sql)")

PROJECT_BRANCH = "agentic/project"
PROJECT_WORKTREE = "project"


def _paths(cfg):
    """ProjectPaths authority. Without a runtime overlay the platform
    repository remains the implicit project (legacy behaviour). With
    `cfg["runtime"]["project_dir"]` (set by the project registry) every
    state directory redirects to the machine-local runtime home while
    prompts/schemas/guardrails stay with the platform installation."""
    runtime = cfg.get("runtime") or {}
    base = runtime.get("project_dir")
    if base:
        return {"agentic": str(base),
                "memory": os.path.join(str(base), "memory"),
                "queue": os.path.join(str(base), "queue"),
                "runs": os.path.join(str(base), "runs"),
                "root": str(config_mod.repo_root(cfg))}
    a = str(config_mod.AGENTIC_DIR)
    return {"agentic": a, "memory": os.path.join(a, "memory"),
            "queue": os.path.join(a, "queue"), "runs": os.path.join(a, "runs"),
            "root": str(config_mod.repo_root(cfg))}


def make_caller(cfg, ledger, board, overrides=None, runner=None,
                transport=None, which=None, env=None, log=None,
                memory_dir=None):
    """Build the single call surface used by every agent role. Every prompt
    is assembled by the Context Broker (ADR 0001) — never ad hoc."""
    from .context.broker import BrokerError
    from .context.compose import compose, retrieval_items, retrieval_query
    from .memsvc import memory_items
    memory_dir = memory_dir or os.path.join(str(config_mod.AGENTIC_DIR),
                                            "memory")

    def _chain_for(role):
        worker_chain = None
        from .routing import REVIEWER_ROLES
        if role in REVIEWER_ROLES and \
                (cfg.get("routing") or {}).get("mode") == "capability":
            worker_chain = backends.routing_chain(
                cfg, "coder", overrides, memory_dir=None, board=board,
                ledger=ledger)
        return backends.routing_chain(cfg, role, overrides,
                                      memory_dir=memory_dir, board=board,
                                      ledger=ledger,
                                      worker_chain=worker_chain)

    def call(role, prompt, input_data=None, schema=None, workspace=None,
             permissions="read", timeout=None, chain=None):
        chain = chain or _chain_for(role)

        def build_prompt(backend_name):
            """Rebuild the full package for a specific backend so fallback
            models get context sized to their own window/budget."""
            retrieved = retrieval_items(cfg, role, input_data, workspace,
                                        memory_dir, runner=runner,
                                        which=which)
            retrieved += memory_items(cfg, memory_dir,
                                      retrieval_query(input_data))
            from .knowledge import knowledge_items
            retrieved += knowledge_items(cfg, str(config_mod.AGENTIC_DIR),
                                         retrieval_query(input_data))
            from .skillreg import skill_items
            retrieved += skill_items(cfg, str(config_mod.AGENTIC_DIR),
                                     role, retrieval_query(input_data))
            package = compose(cfg, role, prompt, input_data, schema,
                              memory_dir=memory_dir, backend=backend_name,
                              extra_items=retrieved)
            return package.rendered

        try:
            rendered = build_prompt(chain[0])
        except BrokerError as exc:
            err = errors.PolicyError("context broker: %s" % exc)
            (log or (lambda e: None))({"event": "context_budget_stop",
                                       "role": role,
                                       "detail": str(exc)[:300]})
            return backends.error_result(chain[0], role, err)
        result = backends.invoke_backend(
            cfg, chain[0], role, rendered, input_data=None,
            output_schema=schema, workspace=workspace,
            permissions=permissions, timeout=timeout, ledger=ledger,
            board=board, fallback_chain=chain[1:], runner=runner,
            transport=transport, which=which, env=env, log=log,
            prompt_builder=build_prompt)
        try:   # honest provider-cache telemetry (Phase 2.F) -- best-effort,
               # never allowed to affect the actual call result
            from . import cachestore
            cache_key = cachestore.compute_cache_key(
                role=role, backend=result.get("backend"),
                model=result.get("model"))
            cachestore.CacheStore(memory_dir).record_provider_cache_observation(
                cache_key, result.get("backend_type"), result.get("usage"))
        except Exception:   # noqa: BLE001
            pass
        return result
    return call


def _context(cfg, memory, ledger=None, board=None, overrides=None,
             caller=None, clock=None, **kw):
    ledger = ledger or capacity_mod.CapacityLedger(cfg, memory, clock=clock)
    board = board or BreakerBoard(memory, clock=clock)
    scheduler = Scheduler(cfg, memory, clock=clock)
    log = lambda event: logs.decision(memory, dict(event, source="project"))
    caller = caller or make_caller(cfg, ledger, board, overrides=overrides,
                                   log=log, memory_dir=memory, **kw)
    return ledger, board, scheduler, caller, log


def _recover_stale_owned_processes(cfg, agentic_dir, root, scheduler, clock,
                                   log):
    """Reconcile process-ownership records left by a run whose OWNING
    Python process was killed outright (not just its child) before its
    own `finally: lease.release(); lock.release()` could run -- the one
    scenario core.supervisor's own bounded-timeout guarantee can never
    prevent by itself. Runs before this cycle even attempts its own
    lock/lease acquisition, so a confirmed-dead prior run's stale state
    never makes a fresh attempt wait out a lease TTL (observed live:
    2026-07-25, ollama-pilot, after a Codex CLI hang killed the owning
    process)."""
    from . import taskspace as _ts

    def _release_lease(record):
        lease = _ts.ProjectLease(agentic_dir, cfg.get("project", {}).get("name"),
                                 clock=clock)
        holder = lease.holder()
        if not holder:
            return
        # Ownership is verified by PID, not run_id: `run_cycle` can
        # acquire the lease before `_run_cycle_locked` has generated
        # its own run_id (a pre-existing gap -- the lease's own
        # `run_id` field is often null), but the OS pid of the
        # process that acquired it always matches this record's
        # `parent_pid` (the process that spawned the supervised CLI
        # call) when they're the same run. Never cleared on a PID
        # match alone without the record's own identity already
        # having been confirmed dead/reconciled by the caller.
        if str(holder.get("pid")) == str(record.get("parent_pid")):
            holder["status"] = "released"
            lease._write(holder)   # noqa: SLF001 -- same file this class owns

    def _release_lock(record):
        lock_path = os.path.join(projstate.project_dir(agentic_dir),
                                 "project.lock")
        if not os.path.exists(lock_path):
            return
        try:
            with open(lock_path, encoding="utf-8") as fh:
                locked_pid = fh.read().strip()
        except OSError:
            return
        if locked_pid and str(record.get("parent_pid")) == locked_pid:
            try:
                os.remove(lock_path)
            except OSError:
                pass

    def _reconcile_task(record):
        task_id = record.get("task_id")
        if not task_id:
            return
        try:
            projstate.update_task(agentic_dir, task_id, status="pending")
        except errors.PolicyError:
            pass
        _ts.release_claim(agentic_dir, task_id)
        if scheduler.state.get("state") == "running" and \
                scheduler.state.get("current_cycle") == record.get("run_id"):
            scheduler.state.update(state="idle", current_cycle=None,
                                   next_run_at=None, cooling_reason=None,
                                   deferred=None)
            scheduler.save()

    recovered = supervisor_mod.recover_stale_owned_processes(
        agentic_dir, root=root, release_lease=_release_lease,
        release_lock=_release_lock, reconcile_task=_reconcile_task, log=log)
    if recovered:
        log({"event": "supervisor_startup_recovery", "count": len(recovered),
            "actions": [r["action"] for r in recovered]})
    return recovered


# -- project start -------------------------------------------------------------

def project_start(cfg, plan_path, caller=None, overrides=None, clock=None,
                  **kw):
    p = _paths(cfg)
    if projstate.exists(p["agentic"]):
        return {"status": "already_started",
                "detail": "project state exists; use project-run/resume or "
                          "delete .agentic/project to restart"}
    with open(plan_path, encoding="utf-8") as fh:
        plan = fh.read()
    ledger, board, scheduler, caller, log = _context(
        cfg, p["memory"], overrides=overrides, caller=caller, clock=clock, **kw)
    # capability inventory (Phase 1.B): built once, before architecture,
    # and persisted -- the architect and every later cycle read it back
    # instead of re-discovering unchanged repository facts each time.
    try:
        from . import registry as _registry_mod
        registry_home = _registry_mod.ProjectRegistry().home
    except Exception:   # noqa: BLE001
        registry_home = None
    try:
        inv = inventory_mod.build_inventory(
            cfg, p["root"], p["agentic"], memory_dir=p["memory"],
            registry_home=registry_home)
        inventory_mod.save(p["agentic"], inv)
        log({"event": "inventory_built",
             "languages": list((inv.get("observed") or {})
                               .get("languages", {}).keys())})
    except Exception as exc:   # noqa: BLE001 -- inventory is best-effort
        log({"event": "inventory_build_failed", "detail": str(exc)[:200]})
    snapshot = _snapshot(p["root"], ["**"])
    result = caller("architect", load_prompt("architect.md", shared=False),
                    {"plan": plan, "repository_files": snapshot["file_list"]},
                    schema=_schema("architect.schema.json"),
                    workspace=p["root"], permissions="read")
    if not result["ok"]:
        err = result.get("error") or {}
        return {"status": "architect_failed", "error": err.get("kind"),
                "detail": err.get("detail"),
                "diagnostic": err.get("diagnostic"),
                "routing_attempts": result.get("routing_attempts", [])}
    out = result["structured_output"]
    a = p["agentic"]
    projstate.write_text(a, "PROJECT.md",
                         "# Project Plan\n\n" + plan)
    projstate.write_text(a, "architecture.md",
                         "# Architecture\n\n" + out["architecture"] +
                         "\n\n## Assumptions\n" +
                         "\n".join("- " + s for s in out.get("assumptions", [])))
    projstate.write_yaml(a, "milestones.yaml",
                         {"milestones": out["milestones"]})
    tasks = [projstate.normalize_task(t) for t in out["backlog"]]
    projstate.save_backlog(a, tasks)
    projstate.write_yaml(a, "acceptance-criteria.yaml", {
        "requirements_map": out.get("requirements_map", []),
        "completion_criteria": out["completion_criteria"]})
    projstate.write_yaml(a, "decisions.yaml", {
        "human_decisions_needed": out.get("human_decisions", []),
        "decided": []})
    projstate.write_yaml(a, "blockers.yaml", {"blockers": []})
    projstate.refresh_progress(a)
    scheduler.set_project_status("in_progress")
    _index_project(cfg, p["root"], p["memory"], log, full=True)
    from .knowledge import update_knowledge
    update_knowledge(cfg, a, log)
    log({"event": "project_started", "tasks": len(tasks),
         "milestones": len(out["milestones"])})
    # reversible implementation preferences (test framework, CSS approach,
    # ...) are resolved autonomously right away -- only what's left after
    # that ever becomes a human blocker (item 6/9: never pause execution
    # for a decision a human never needed to make).
    resolved = decision_policy.auto_resolve_reversible_decisions(a, p["root"])
    if resolved:
        log({"event": "reversible_decisions_auto_resolved",
             "decisions": [r["decision"] for r in resolved]})
    remaining = projstate.read_yaml(
        a, "decisions.yaml", {}).get("human_decisions_needed", [])
    for decision in remaining:
        projstate.add_blocker(a, None, decision, human_only=True,
                              code=projstate.BLOCKER_CODE_GENUINE_HUMAN_DECISION)
    return {"status": "started", "tasks": len(tasks),
            "milestones": len(out["milestones"]),
            "human_decisions": remaining}


def _remember(cfg, memory_dir, rtype, title, summary, **kw):
    """Deterministic, best-effort memory write. Never breaks a cycle."""
    try:
        from .memsvc import get_memory, memory_config
        if not memory_config(cfg)["enabled"]:
            return None
        return get_memory(cfg, memory_dir).save(rtype, title, summary, **kw)
    except Exception:   # noqa: BLE001
        return None


def _index_project(cfg, root, memory_dir, log, full, changed=None):
    """Best-effort code-intelligence indexing; never fails the cycle."""
    from .codeintel import ci_config, get_adapter
    cicfg = ci_config(cfg)
    want = cicfg["index_on_project_start"] if full \
        else cicfg["incremental_after_commit"]
    if not want:
        return
    try:
        adapter = get_adapter(cfg, root, memory_dir)
        if full:
            result = adapter.index_full()
        else:
            revision = gitops.run_git(["rev-parse", "HEAD"], cwd=root,
                                      check=False).strip() or None
            result = adapter.index_changes(changed or [], revision)
        log({"event": "code_index", "full": full,
             "provider": result.get("provider"),
             "files_indexed": result.get("files_indexed")})
    except Exception as exc:   # noqa: BLE001 — indexing is best-effort
        log({"event": "code_index_failed", "detail": str(exc)[:200]})


# -- worktree ---------------------------------------------------------------------

def ensure_project_worktree(cfg, p):
    """One persistent worktree on the agentic/project branch, reused across
    cycles so the application accumulates. The user's tree is never touched;
    merging to main is always the human's act."""
    path = os.path.join(p["agentic"], "worktrees", PROJECT_WORKTREE)
    if os.path.exists(os.path.join(path, ".git")):
        return path
    if not gitops.has_commits(p["root"]):
        raise errors.PolicyError("repository has no commits; commit first")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    branches = gitops.run_git(["branch", "--list", PROJECT_BRANCH],
                              cwd=p["root"], check=False)
    if PROJECT_BRANCH.split("/")[-1] in branches or PROJECT_BRANCH in branches:
        gitops.run_git(["worktree", "add", path, PROJECT_BRANCH],
                       cwd=p["root"])
    else:
        gitops.run_git(["worktree", "add", "-b", PROJECT_BRANCH, path, "HEAD"],
                       cwd=p["root"])
    return path


# -- security trigger ----------------------------------------------------------------

def security_review_required(task, changed_files, diff_text):
    if task.get("security_relevant"):
        return True
    for path in changed_files:
        if gitops.matches_any(path, SECURITY_PATH_TRIGGERS):
            return True
    return bool(SECURITY_DIFF_RE.search(diff_text or ""))


# -- one cycle -------------------------------------------------------------------------

def run_cycle(cfg, caller=None, overrides=None, clock=None, run_id=None,
              **kw):
    p = _paths(cfg)
    a = p["agentic"]
    if not projstate.exists(a):
        return {"status": "no_project", "detail": "run project-start first"}
    ledger, board, scheduler, caller, log = _context(
        cfg, p["memory"], overrides=overrides, caller=caller, clock=clock, **kw)
    _recover_stale_owned_processes(cfg, a, p["root"], scheduler, clock, log)
    cycle_minutes = ((cfg.get("scheduler") or {}).get("cycle") or {}).get(
        "maximum_duration_minutes",
        (cfg.get("cycle") or {}).get("maximum_duration_minutes"))
    ok, reason = scheduler.eligible(cycle_minutes=cycle_minutes)
    if not ok:
        return {"status": "not_eligible", "reason": reason,
                "next_run_at": scheduler.state.get("next_run_at")}

    lock = projstate.ProjectLock(a)
    if not lock.acquire():
        return {"status": "locked", "detail": "another cycle is running"}
    if lock.broke_stale_lock:
        log({"event": "stale_lock_recovered", "run_id": run_id,
             "age_seconds": lock.broke_stale_lock_age_seconds})
    from . import taskspace
    lease = taskspace.ProjectLease(a, cfg.get("project", {}).get("name"),
                                   clock=clock)
    acquired, holder = lease.acquire(run_id=run_id)
    if not acquired:
        lock.release()
        return {"status": "lease_held",
                "detail": "project lease held by %s (pid %s) until %s"
                          % (holder.get("machine_id"), holder.get("pid"),
                             holder.get("expires_at")),
                "holder": {k: holder.get(k) for k in
                           ("machine_id", "pid", "run_id", "expires_at")}}
    try:
        return _run_cycle_locked(cfg, p, ledger, board, scheduler, caller,
                                 log, overrides, run_id)
    finally:
        lease.release()
        lock.release()
        supervisor_mod.clear_run_context()


def _finish_cycle(cfg, p, scheduler, ledger, log, run_id, task, backend,
                  outcome, tokens, started_at, detail="", retry_after=None,
                  failure_class=None):
    duration = int((_dt.datetime.now() - started_at).total_seconds())
    ledger.record_cycle(run_id, backend or "-",
                        (task or {}).get("skill") or (task or {}).get("id", "-"),
                        (task or {}).get("expected_size", "medium"),
                        tokens, duration, outcome)
    cool_outcome = outcome if outcome in ("success", "rate_limit",
                                          "usage_limit") else "failure"
    # a platform-classified failure (see core.failures) never escalates
    # the ordinary model/provider failure-cooldown streak -- it gets its
    # own short, flat cooldown instead (scheduler.cooldown_breakdown).
    cool_outcome = failures.cooling_outcome_for(failure_class, cool_outcome) \
        or cool_outcome
    until = scheduler.start_cooling(cool_outcome,
                                    retry_after_seconds=retry_after)
    _remember(cfg, p["memory"], "cycle_outcome",
              "cycle %s: %s" % (run_id, outcome),
              (detail or outcome)[:400], task_id=(task or {}).get("id"),
              cycle_id=run_id, source="cycle",
              importance=0.6 if outcome == "success" else 0.7)
    cooling_detail = scheduler.state.get("cooling_detail")
    log({"event": "cycle_finished", "run_id": run_id, "outcome": outcome,
         "detail": redact(str(detail))[:300],
         "cooling_until": until.isoformat(timespec="seconds"),
         "cooling_detail": cooling_detail})
    try:
        gate_events = featuregates.FeatureGateRegistry(
            p["memory"], cfg).record_outcome(
                run_id, outcome,
                platform_failure=failures.is_platform_class(failure_class),
                detail=detail)
        for event in gate_events:
            log(dict(event, event="feature_gate_rollback", run_id=run_id))
    except Exception as exc:  # evidence must never alter cycle outcome
        log({"event": "feature_gate_outcome_failed", "run_id": run_id,
             "detail": str(exc)[:200]})
    progress = projstate.refresh_progress(p["agentic"])
    from .knowledge import update_knowledge
    update_knowledge(cfg, p["agentic"], log)
    result = {"status": outcome, "run_id": run_id,
              "task": (task or {}).get("id"), "detail": detail,
              "cooling_until": until.isoformat(timespec="seconds"),
              "cooling_detail": cooling_detail,
              "progress": progress}
    if notify.should_notify(cfg, "cycle_complete"):
        notify.notify(cfg, "cycle_complete", "Cycle %s: %s"
                      % (run_id, outcome),
                      "task=%s %s" % ((task or {}).get("id"), detail),
                      p["memory"])
    return result


def _run_cycle_locked(cfg, p, ledger, board, scheduler, caller, log,
                      overrides, run_id):
    a = p["agentic"]
    run_id = run_id or _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    supervisor_mod.set_run_context(
        a, run_id, project_id=cfg.get("project", {}).get("name"))
    started_at = _dt.datetime.now()
    run_dir = os.path.join(p["runs"], "cycle-" + run_id)
    os.makedirs(run_dir, exist_ok=True)
    # guardrails are OS policy: always read from the platform install,
    # even when project state is redirected to the runtime home
    protected = gitops.load_protected_paths(cfg, str(config_mod.AGENTIC_DIR))
    # Capability Plan (Phase 3) can narrow exactly two protected-path
    # categories (Supabase migrations, Docker files) -- only once it has
    # actually selected the capability that needs them; see Phase 0
    # decision, capability-intelligence-design.md section 3.
    capability_plan = projstate.read_yaml(a, "capability-plan.yaml", None)
    authorised_exceptions = gitops.capability_authorised_exceptions(
        capability_plan)
    # -- persisted-state recovery: runs BEFORE any human-blocker gate below,
    # so a project stuck on a since-fixed platform bug (or on a decision
    # that was never actually a human's to make) self-heals on its own
    # next cycle instead of surfacing a stale human_required forever.
    try:   # restart recovery: surface abandoned task worktrees
        from . import taskspace as _ts
        abandoned = _ts.recover_abandoned(p["root"], a)
        if abandoned:
            log({"event": "abandoned_worktrees", "run_id": run_id,
                 "worktrees": abandoned})
    except Exception:   # noqa: BLE001 — recovery is best-effort
        pass
    try:   # self-heal tasks stuck on the pre-fix zero-check deadlock
        recovered = bootstrap_gate.recover_bootstrap_deadlock(a)
        if recovered:
            log({"event": "bootstrap_deadlock_recovered", "run_id": run_id,
                 "recovered": recovered})
    except Exception:   # noqa: BLE001 — recovery is best-effort
        pass
    try:   # self-heal tasks stuck on the pre-fix expected_paths-as-
           # allowlist contract bug (run 20260722-181213)
        recovered_contract = bootstrap_gate.recover_expected_paths_contract_bug(a)
        if recovered_contract:
            log({"event": "expected_paths_contract_bug_recovered",
                 "run_id": run_id, "recovered": recovered_contract})
    except Exception:   # noqa: BLE001 — recovery is best-effort
        pass
    try:   # self-heal tasks stuck on the legacy aggregate Phase-5
           # "parallel candidates exhausted" blocker (code=None) once its
           # constituent causes are conclusively platform-owned
        recovered_aggregate = \
            parallel_recovery.recover_parallel_candidate_aggregate(a)
        if recovered_aggregate:
            log({"event": "parallel_candidate_aggregate_recovered",
                 "run_id": run_id,
                 "recovered": [e for e in recovered_aggregate
                              if e.get("recovered")]})
    except Exception:   # noqa: BLE001 — recovery is best-effort
        pass
    try:   # self-heal tasks stuck on the legacy canonical-contract-
           # divergence defect (backlog expected_paths narrower than what
           # the work order actually required -- see core.contract_recovery)
        recovered_contract_divergence = \
            contract_recovery.recover_contract_divergence_blockers(
                a, p["memory"], cfg)
        if recovered_contract_divergence:
            log({"event": "contract_divergence_recovered", "run_id": run_id,
                 "recovered": recovered_contract_divergence})
    except Exception:   # noqa: BLE001 — recovery is best-effort
        pass
    try:   # auto-resolve reversible technical choices left over from a
           # project started before this policy existed
        resolved = decision_policy.auto_resolve_reversible_decisions(
            a, p["root"])
        if resolved:
            log({"event": "reversible_decisions_auto_resolved",
                 "run_id": run_id,
                 "decisions": [r["decision"] for r in resolved]})
    except Exception:   # noqa: BLE001 — recovery is best-effort
        pass
    if scheduler.state.get("project_status") == "blocked_on_human" and \
            not projstate.open_blockers(a, human_only=True):
        scheduler.set_project_status("in_progress")
    try:   # refresh the capability inventory ONLY if it's actually stale
        if inventory_mod.is_stale(a, p["root"]):
            from . import registry as _registry_mod
            try:
                registry_home = _registry_mod.ProjectRegistry().home
            except Exception:   # noqa: BLE001
                registry_home = None
            inventory_mod.ensure_inventory(
                cfg, p["root"], a, memory_dir=p["memory"],
                registry_home=registry_home, force=True)
            log({"event": "inventory_refreshed", "run_id": run_id})
    except Exception:   # noqa: BLE001 — inventory refresh is best-effort
        pass

    task = projstate.next_task(a)
    if task is None:
        human = projstate.open_blockers(a, human_only=True)
        progress = projstate.refresh_progress(a)
        if progress.get("backlog_complete"):
            return final_audit(cfg, caller=caller, clock=None,
                               _preloaded=(ledger, board, scheduler, log))
        if human:
            notify.notify(cfg, "human_blocker", "Human decision needed",
                          "; ".join(b["reason"] for b in human)[:300],
                          p["memory"])
            scheduler.set_project_status("blocked_on_human")
            return {"status": "human_required",
                    "blockers": [b["reason"] for b in human]}
        return {"status": "blocked",
                "detail": "no eligible task (dependencies blocked)"}

    # capacity gate ------------------------------------------------------------
    chain = backends.routing_chain(cfg, "coder", overrides, board=board,
                                   ledger=ledger)
    decision = capacity_mod.decide_start(cfg, task, ledger, board, chain)
    log({"event": "capacity_decision", **decision})
    if decision["decision"] == "wait":
        until = decision.get("wait_until")
        scheduler.defer(decision["reason"],
                        decision.get("required_estimated_tokens"),
                        decision.get("confidence"), until)
        return {"status": "waiting_capacity", "until": until,
                "reason": decision["reason"]}
    if decision["decision"] == "human_required":
        notify.notify(cfg, "backends_unavailable", "No usable backend",
                      decision["reason"], p["memory"])
        return {"status": "human_required", "reason": decision["reason"]}
    backend = decision["selected_backend"]
    coder_chain = [backend] + [b for b in chain if b != backend]
    scheduler.begin_cycle(run_id, backend)
    projstate.update_task(a, task["id"], status="in_progress")

    def fail(outcome, detail, retry_after=None, block=False,
             blocking_reason=None, human_only=False, code=None,
             failure_class=None, platform_owned=None, retryable=None,
             evidence_ref=None, candidate_failures=None):
        from . import taskspace as _ts
        _ts.release_claim(a, task["id"])   # failed worktree stays as evidence
        projstate.update_task(
            a, task["id"],
            status="blocked" if block else "pending",
            attempts=task["attempts"] + 1, last_result=outcome,
            blocking_reason=blocking_reason or (detail[:200] if block else None))
        if block:
            # the ONLY place a blocker is recorded for a task failure --
            # never duplicate this with a second add_blocker call at the
            # call site, or a legacy human_only=True/False duplicate pair
            # (like the one this fixed) reappears. Every new blocker gets
            # a full identity (item 8): platform_owned/retryable default
            # from the stable failure taxonomy when the call site didn't
            # already know them explicitly -- never left to infer later.
            policy = failures.policy_for(failure_class) if failure_class \
                else None
            memory_id = _remember(cfg, p["memory"], "failed_attempt",
                                  "task %s blocked" % task["id"],
                                  (blocking_reason or detail)[:400],
                                  task_id=task["id"], cycle_id=run_id,
                                  source="cycle", importance=0.8)
            projstate.add_blocker(
                a, task["id"], blocking_reason or detail,
                human_only=human_only, code=code,
                failure_class=failure_class,
                platform_owned=(failures.is_platform_class(failure_class)
                                if platform_owned is None and failure_class
                                else platform_owned),
                retryable=(policy["retry_policy"] != failures.RETRY_NEVER_AUTOMATIC
                          if retryable is None and policy else retryable),
                evidence_ref=evidence_ref,
                candidate_failures=candidate_failures,
                memory_record_id=memory_id)
        if failure_class:
            log({"event": "failure_classified", "run_id": run_id,
                 "task_id": task["id"], "failure_class": failure_class,
                 "platform_class": failures.is_platform_class(failure_class)})
        _persist_evidence(run_dir, "failure-classification.json",
                          {"task_id": task["id"], "outcome": outcome,
                           "failure_class": failure_class,
                           "platform_class": failures.is_platform_class(
                               failure_class) if failure_class else None,
                           "blocked": block,
                           "blocking_reason": blocking_reason or
                           (detail[:200] if block else None)})
        return _finish_cycle(cfg, p, scheduler, ledger, log, run_id, task,
                             backend, outcome, 0, started_at, detail,
                             retry_after, failure_class=failure_class)

    # conductor -------------------------------------------------------------------
    project_worktree = ensure_project_worktree(cfg, p)
    feature_registry = featuregates.FeatureGateRegistry(p["memory"], cfg)
    amendment_gate = feature_registry.decision(
        "contract_amendments", cfg.get("project", {}).get("name"))
    contract_seed = contract_mod.build_task_contract(
        task, {}, cfg.get("project", {}).get("name"), run_id=run_id)
    conducted = caller(
        "conductor", load_prompt("project-conductor.md", shared=False),
        {"task": task,
         "task_contract": contract_seed,
         "contract_authority": "backlog",
         "feature_gates": {"contract_amendments": amendment_gate},
         "architecture": (projstate.read_yaml(a, "progress.yaml", {}) or {}),
         "repository_files": _snapshot(project_worktree,
                                       ["**"])["file_list"][:300],
         "limits": {"max_changed_lines":
                    cfg.get("execution", {}).get("max_changed_lines", 400)}},
        schema=_schema("work-order.schema.json"), workspace=project_worktree,
        permissions="read")
    if not conducted["ok"]:
        kind = (conducted.get("error") or {}).get("kind", "?")
        retry = (conducted.get("capacity") or {}).get("retry_after_seconds")
        return fail(kind if kind in ("rate_limit", "usage_limit")
                    else "failure", "conductor failed: %s" % kind, retry,
                    failure_class=failures.EXECUTION_TIMEOUT
                    if kind == "timeout" else None)
    proposed_order = conducted["structured_output"]
    _persist_evidence(run_dir, "conductor-work-order.json", proposed_order)
    feature_registry.begin_run(
        run_id, {"contract_amendments": amendment_gate}
        if proposed_order.get("contract_amendments") else {})
    _persist_evidence(
        run_dir, "feature-gates.json",
        {"contract_amendments": amendment_gate})
    order = contract_mod.canonicalize_work_order(
        task, proposed_order, feature_gate=amendment_gate)
    with open(os.path.join(run_dir, "work-order.json"), "w",
              encoding="utf-8") as fh:
        json.dump(order, fh, indent=2)
    if order["action"] == "queue":
        return fail("failure", "conductor queued: %s" % order.get("queue_reason"),
                    block=True, blocking_reason=order.get("queue_reason"))
    for pattern in order.get("allowed_paths", []):
        if gitops.pattern_is_protected(pattern, protected,
                                       authorised_exceptions):
            return fail("failure", "work order grants protected path %s"
                        % pattern, block=True,
                        blocking_reason="protected path in work order",
                        code=projstate.BLOCKER_CODE_POLICY_DENIED,
                        failure_class=failures.WORKSPACE_POLICY_DENIED)

    worker_role = _worker_role(task, order)
    order = _enrich_work_order_safe(cfg, p, a, order, task, worker_role,
                                    capability_plan, ledger, log, run_id)
    # Capability/skill enrichment may attach execution metadata, but stable
    # contract authority is re-applied afterward so no extension can mutate
    # outputs, acceptance criteria, checks, or their writable coverage.
    order = contract_mod.canonicalize_work_order(
        task, order, feature_gate=amendment_gate)
    with open(os.path.join(run_dir, "contract-amendments.json"), "w",
              encoding="utf-8") as fh:
        json.dump({
            "authority": order.get("contract_authority"),
            "feature_gate": amendment_gate,
            "policy": task.get("contract_amendment_policy") or {
                "enabled": False},
            "proposals": order.get("contract_amendments") or [],
            "decisions": order.get("contract_amendment_decisions") or [],
        }, fh, indent=2, default=str)
    with open(os.path.join(run_dir, "work-order.json"), "w",
              encoding="utf-8") as fh:
        json.dump(order, fh, indent=2)

    # file-ownership claim + isolated per-task worktree ------------------------
    from . import taskspace
    try:
        taskspace.claim_paths(a, task["id"], order.get("allowed_paths", [])
                              + bootstrap_gate.expected_path_strings(task),
                              run_id=run_id)
    except errors.PolicyError as exc:
        return fail("failure", "ownership conflict: %s" % exc.detail,
                    block=True, blocking_reason=exc.detail[:200],
                    failure_class=failures.PROJECT_DEPENDENCY_MISSING)
    worktree = taskspace.create_task_worktree(p["root"], a, task["id"],
                                              PROJECT_BRANCH)
    supervisor_mod.set_run_context(
        a, run_id, project_id=cfg.get("project", {}).get("name"),
        task_id=task["id"], worktree=worktree)
    # overwritten below only when Phase 5 parallel candidates ran and a
    # candidate OTHER than this primary worktree won -- the single-agent
    # path (the overwhelming majority of tasks) never touches this.
    integration_task_id = task["id"]
    # True only when parallel candidates tied on every ranking signal:
    # the tie-broken winner (lowest-index candidate) still goes through
    # security review and checks normally, but the final merge into
    # agentic/project is held for a human decision instead of guessed.
    integration_requires_human = False
    # revalidate-before-regenerate (item 9): `create_task_worktree` resumes
    # a preserved worktree from a previous, non-reverted failure (e.g. a
    # task the coder itself blocked on, or one recovered from a since-fixed
    # platform bug) rather than starting fresh. If it already has changes,
    # give the EXISTING work a chance to pass the (now-current) checks
    # before asking the model to redo it -- never discarded on first
    # contact with a stale block.
    #
    # BUT only when that preserved content is still actually compatible
    # with THIS cycle's work order: a fresh conductor call can legitimately
    # narrow allowed_paths from a previous attempt, and a worker has no
    # way to resolve that itself -- even a "delete" action requires the
    # path to be in allowed_paths, so an out-of-scope leftover file is an
    # unsolvable catch-22 for the model (observed live: run 20260723-225324
    # against ollama-pilot). When preserved content no longer fits the
    # current scope, the platform resets the worktree itself rather than
    # handing the model an impossible cleanup task.
    gitops.stage_all(worktree)
    preserved_files = gitops.filter_tool_artifacts(
        gitops.changed_files(worktree))
    preserved_work = bool(preserved_files)
    if preserved_work:
        incompatible = gitops.check_paths(
            preserved_files, order.get("allowed_paths", []),
            order.get("forbidden_paths", []), protected,
            authorised_exceptions=authorised_exceptions)
        if incompatible:
            log({"event": "preserved_work_incompatible_with_scope",
                 "run_id": run_id, "task_id": task["id"],
                 "reasons": incompatible[:5]})
            _revert_worktree(worktree)
            preserved_work = False
        else:
            log({"event": "revalidating_preserved_work", "run_id": run_id,
                 "task_id": task["id"]})

    # feasibility preflight (Phase 1.C) ------------------------------------------
    # runs entirely in code, after the worktree exists but BEFORE the coder
    # is ever invoked -- a platform_invalid/dependency_wait/human_required/
    # credential_required result never consumes model capacity.
    task_contract = contract_mod.build_task_contract(
        task, order, cfg.get("project", {}).get("name"), run_id=run_id)
    # soft evidence only (never a dispatch gate -- see
    # find_acceptance_criteria_gaps's own docstring): persisted alongside
    # the contract for diagnosis, never consulted by preflight.
    task_contract["acceptance_criteria_gaps"] = \
        contract_mod.find_acceptance_criteria_gaps(task_contract)
    # persist the ONE compiled contract every downstream component (gate,
    # reviewer, recovery) can be pointed back to as evidence (item 4) --
    # written BEFORE preflight so even a platform_invalid short-circuit
    # leaves the compiled contract on disk for diagnosis.
    with open(os.path.join(run_dir, "task-contract.json"), "w",
              encoding="utf-8") as fh:
        json.dump(task_contract, fh, indent=2, default=str)
    new_contract_hash = contract_mod.contract_hash(task_contract)
    if task.get("contract_hash") and \
            task["contract_hash"] != new_contract_hash:
        # the compiled contract changed since this task was last attempted
        # (e.g. a canonical-contract-divergence migration) -- any
        # prompt/context-cache entry keyed to THIS task's OLD contract
        # must never be served again (item 6). Task-scoped dependency
        # name: never invalidates another task's cache entries.
        from . import cachestore as _cachestore
        invalidated = _cachestore.CacheStore(p["memory"]).invalidate_dependents(
            "task_contract_hash:%s" % task["id"], new_contract_hash,
            reason="task %s contract changed" % task["id"])
        if invalidated:
            log({"event": "task_contract_cache_invalidated", "run_id": run_id,
                 "task_id": task["id"], "invalidated_keys": invalidated})
    projstate.update_task(a, task["id"], contract_hash=new_contract_hash)
    remaining_decisions = projstate.read_yaml(
        a, "decisions.yaml", {}).get("human_decisions_needed", [])
    preflight_result = preflight_mod.run_preflight(
        task_contract, task, projstate.load_backlog(a), worktree, a,
        decisions_needed=remaining_decisions, capacity_decision=decision,
        backend=backend, inventory=inventory_mod.load(a))
    log({"event": "preflight", "run_id": run_id, "task_id": task["id"],
         "result": preflight_result["result"],
         "checks": preflight_result["checks"]})
    if not preflight_result["consumes_capacity"]:
        result_kind = preflight_result["result"]
        reason = "; ".join(c["detail"] for c in preflight_result["checks"]
                           if not c["ok"]) or result_kind
        if result_kind == preflight_mod.RESULT_HUMAN_REQUIRED:
            return fail("failure", "preflight: %s" % reason, block=True,
                        human_only=True,
                        blocking_reason="preflight human_required: %s"
                        % reason[:200],
                        code=projstate.BLOCKER_CODE_GENUINE_HUMAN_DECISION,
                        failure_class=failures.GENUINE_HUMAN_DECISION)
        if result_kind == preflight_mod.RESULT_CREDENTIAL_REQUIRED:
            return fail("failure", "preflight: %s" % reason, block=True,
                        human_only=True,
                        blocking_reason="preflight credential_required: %s"
                        % reason[:200],
                        code=projstate.BLOCKER_CODE_AUTHENTICATION_REQUIRED,
                        failure_class=failures.PROVIDER_AUTHENTICATION)
        if result_kind == preflight_mod.RESULT_DEPENDENCY_WAIT:
            return fail("failure", "preflight: %s" % reason,
                        failure_class=failures.PROJECT_DEPENDENCY_MISSING)
        # platform_invalid / replan_required: a genuine platform-side
        # contract/capability problem -- block for a human/architect to
        # fix, never silently spin.
        return fail("failure", "preflight %s: %s" % (result_kind, reason),
                    block=True,
                    blocking_reason=("preflight %s: %s"
                                     % (result_kind, reason))[:200],
                    code=projstate.BLOCKER_CODE_STRUCTURAL_CONTRACT_MISMATCH,
                    failure_class=failures.TASK_CONTRACT_INVALID)

    # coder + deterministic checks + bounded repair/review loops ------------------
    # Two separate bounds (Phase 7): deterministic repair attempts
    # (repair.maximum_attempts_per_task, default 3) and model-review repair
    # rounds (cycle.maximum_review_rounds, default 2). Failure fingerprints
    # short-circuit hopeless identical retries; every escalation persists a
    # blocker and a memory record.
    repair_cfg = cfg.get("repair") or {}
    max_det_attempts = int(repair_cfg.get("maximum_attempts_per_task", 3))
    max_review_rounds = int((cfg.get("cycle") or {}).get(
        "maximum_review_rounds", repair_cfg.get("maximum_review_rounds", 2)))
    gate_result = None
    qa_out = None
    det_attempts = 0
    review_rounds = 0
    coder_calls = 0
    seen_fingerprints = set()
    failed_backends = set()
    feedback = None
    used_backend = backend
    total_tokens = 0
    # execution-engine selection (Phase 4): decided ONCE per cycle, not
    # per repair attempt -- native today (Orca is opt-in, disabled by
    # default, and falls back to native automatically when absent or
    # incompatible; see core.execengine). Everything else in this loop
    # (task selection, contract, allowed_paths, verification, review,
    # completion, memory, cooling, recovery) is unaffected by this
    # choice -- the engine only ever produces the coder's edits.
    try:
        engine, engine_decision = execengine.select_engine(cfg, caller=caller)
    except execengine.EngineUnavailable as exc:
        # only reachable when an admin explicitly disabled
        # fallback_to_native AND orca is genuinely unavailable -- a
        # deliberate hard-fail choice, never the default behaviour.
        return fail("failure", "execution engine unavailable: %s" % exc,
                    block=True,
                    blocking_reason=("execution engine unavailable: %s"
                                     % exc)[:200],
                    failure_class=failures.PLATFORM_CAPABILITY_MISSING)
    log({"event": "execution_engine_selected", "run_id": run_id,
        "task_id": task["id"], **engine_decision})
    parallel_n, parallel_signals = parallel_mod.decide_agent_count(
        task, cfg)
    if parallel_n <= 1:
        while True:
            coder_calls += 1
            if coder_calls == 1 and preserved_work:
                # skip the coder entirely this pass -- let the checks below
                # run against what's already there
                result = {"ok": True, "backend": used_backend, "edits": None,
                          "blocked": False, "blocker": None, "usage": {}}
            else:
                coder_input = {"work_order": order}
                if feedback:
                    coder_input.update(feedback)   # structured repair/handoff packet
                chain_now = coder_chain
                if feedback and feedback.get("handoff"):
                    chain_now = feedback["handoff_chain"]
                session = engine.launch_agent(execengine.SessionRequest(
                    run_id=run_id, task_id=task["id"], worktree=worktree,
                    role=worker_role, coder_input=coder_input,
                    chain=chain_now))
                result = session.to_legacy_dict()
            # persisted evidence (item 4): backend response metadata and
            # the redacted structured result, every attempt -- never lost
            # again regardless of how this attempt ultimately resolves.
            _persist_evidence(run_dir, "backend-metadata-%d.json"
                              % coder_calls,
                              {"backend": result.get("backend"),
                               "model": result.get("model"),
                               "usage": result.get("usage"),
                               "finish_reason": result.get("finish_reason"),
                               "capacity": result.get("capacity"),
                               "error": result.get("error")})
            _persist_evidence(run_dir, "coder-result-%d.json" % coder_calls,
                              result)
            if not result["ok"]:
                kind = (result.get("error") or {}).get("kind", "?")
                retry = (result.get("capacity") or {}).get("retry_after_seconds")
                if kind in ("rate_limit", "usage_limit"):
                    # backend down mid-task: structured handoff to the next
                    # backend, each backend tried at most once
                    failed_backends.add(result.get("backend"))
                    remaining = [b for b in coder_chain
                                 if b not in failed_backends]
                    if remaining:
                        feedback = _handoff_payload(order, worktree, gate_result,
                                                    remaining)
                        used_backend = remaining[0]
                        log({"event": "handoff", "run_id": run_id,
                             "from": result.get("backend"), "to": remaining[0]})
                        continue
                return fail(kind if kind in ("rate_limit", "usage_limit")
                            else "failure", "coder failed: %s" % kind, retry,
                            failure_class=failures.EXECUTION_TIMEOUT
                            if kind == "timeout" else None)
            used_backend = result.get("backend", used_backend)
            usage = result.get("usage") or {}
            total_tokens += (usage.get("input_tokens") or 0) + \
                            (usage.get("output_tokens") or 0)
            if result.get("blocked"):
                # never trust a model-declared blocker blindly (item 5):
                # a claim that a platform-provided primitive (mkdir, file
                # write, git init, ...) is unavailable is ALWAYS
                # contradicted -- `fscap`'s capability layer needs no
                # environment probing to prove those exist. One
                # corrective repair round is permitted before this
                # actually blocks the task, and the contradicted claim's
                # own free text is never copied verbatim into
                # blocking_reason.
                claim = result.get("blocker") or "coder blocked"
                contradicted = fscap.contradicted_capability_claim(claim)
                if contradicted and det_attempts < max_det_attempts:
                    det_attempts += 1
                    log({"event": "contradicted_capability_claim",
                         "run_id": run_id, "task_id": task["id"],
                         "capability": contradicted, "claim": claim[:300]})
                    feedback = {
                        "failing_checks": [],
                        "capability_contradiction": {
                            "capability": contradicted,
                            "detail": ("the platform's capability layer "
                                      "(core.fscap) proves %s is always "
                                      "available; do not repeat this "
                                      "claim, use the provided edit/mkdir "
                                      "actions instead" % contradicted)},
                        "instruction": "your prior claim is contradicted "
                                      "by the platform's own capability "
                                      "layer -- retry using the edit "
                                      "actions already available to you"}
                    continue
                if contradicted:
                    _revert_worktree(worktree)
                    return fail(
                        "failure",
                        "coder repeated a contradicted capability claim "
                        "(%s)" % contradicted, block=True,
                        blocking_reason=(
                            "contradicted capability claim: %s -- the "
                            "platform capability layer proves this is "
                            "available" % contradicted)[:200],
                        code=projstate.BLOCKER_CODE_STRUCTURAL_CONTRACT_MISMATCH,
                        failure_class=failures.MODEL_OUTPUT_INVALID)
                return fail("failure", claim, block=True,
                            blocking_reason=claim)

            violations = _apply_and_check_paths(cfg, result, order, worktree,
                                                protected, authorised_exceptions,
                                                log=log, run_dir=run_dir,
                                                attempt=coder_calls)
            if violations:
                det_attempts += 1
                log({"event": "scope_violation", "run_id": run_id,
                     "violations": violations[:5]})
                if det_attempts >= max_det_attempts:
                    _revert_worktree(worktree)
                    return fail("failure", "scope violations: %s"
                                % "; ".join(violations[:3]), block=True,
                                blocking_reason="repeated scope violations")
                feedback = {"failing_checks": [],
                            "scope_violations": violations,
                            "instruction": "revert or move out-of-scope changes"}
                continue

            bootstrap_ok, _bootstrap_reason = \
                bootstrap_gate.bootstrap_eligible(
                    task, projstate.load_backlog(a),
                    bootstrap_gate.decisions_text(a))
            task_checks = [] if bootstrap_ok else \
                (task_contract.get("deterministic_checks") or [])
            gate_result = gate.run_checks(
                cfg, worktree,
                os.path.join(run_dir, "checks-%d" % coder_calls),
                required_commands=task_checks)
            if gate_result["no_checks"]:
                # "no checks configured" is NEVER a pass -- but a task the
                # architect itself classified as bootstrap/scaffolding, in a
                # project whose backlog already commits to a later test-setup
                # task, gets a deterministic structural gate instead of an
                # instant block. Its result is folded into the SAME repair
                # loop below (never a bare pass): it reports
                # tests=not_configured_yet, never tests=passed, and any
                # failure gets the normal repair attempts before blocking --
                # generated work is no longer discarded on first contact.
                eligible, reason = bootstrap_gate.bootstrap_eligible(
                    task, projstate.load_backlog(a),
                    bootstrap_gate.decisions_text(a))
                if eligible:
                    gate_result = bootstrap_gate.run_structural_checks(
                        task, worktree,
                        os.path.join(run_dir,
                                     "checks-%d-bootstrap" % coder_calls))
                    log({"event": "bootstrap_structural_gate", "run_id": run_id,
                         "task_id": task["id"], "ok": gate_result["ok"],
                         "tests": gate_result["tests"]})
                else:
                    _revert_worktree(worktree)
                    # a single add_blocker call (inside fail()) -- the
                    # pre-fix duplicate (one human_only=True blocker recorded
                    # here PLUS a second human_only=False one from fail()'s
                    # own add_blocker) is exactly the bug the live pilot hit.
                    return fail(
                        "failure", "zero deterministic checks: blocking",
                        block=True, human_only=True,
                        blocking_reason=bootstrap_gate.NO_CHECKS_HUMAN_REASON,
                        code=bootstrap_gate.DETERMINISTIC_CHECKS_MISSING_CODE,
                        failure_class=failures.PLATFORM_CAPABILITY_MISSING)
            _persist_evidence(run_dir, "validation-result-%d.json"
                              % coder_calls, gate_result)
            if not gate_result["ok"]:
                failing = [r for r in gate_result["results"]
                           if r["mandatory"] and not r["passed"]]
                fingerprint = _failure_fingerprint(failing, worktree)
                if fingerprint in seen_fingerprints:
                    _revert_worktree(worktree)
                    log({"event": "repeated_identical_failure",
                         "run_id": run_id, "fingerprint": fingerprint})
                    return fail("failure",
                                "repeated identical failure — stopping early",
                                block=True,
                                blocking_reason="repeated identical failure "
                                                "(same diff, same errors)")
                seen_fingerprints.add(fingerprint)
                det_attempts += 1
                log({"event": "gate_failed", "run_id": run_id,
                     "attempt": det_attempts,
                     "failing": [r["name"] for r in failing]})
                if det_attempts >= max_det_attempts:
                    _revert_worktree(worktree)
                    return fail("failure",
                                "deterministic checks failing after %d attempts"
                                % det_attempts, block=True,
                                blocking_reason="repair attempts exhausted")
                feedback = {"failing_checks":
                            [{"name": r["name"], "detail": r["detail"][:400]}
                             for r in failing],
                            "instruction": "make the failing checks pass; do not "
                                           "weaken or delete tests"}
                continue

            # QA review (independent, fresh context) --------------------------------
            qa_input = _review_input(order, worktree, gate_result, task)
            qa = caller("qa", load_prompt("qa-review.md", shared=False), qa_input,
                        schema=_schema("verification.schema.json"),
                        workspace=worktree, permissions="read")
            qa_out = qa["structured_output"] if qa["ok"] else None
            verdict = (qa_out or {}).get("verdict", "uncertain")
            log({"event": "qa_review", "run_id": run_id, "verdict": verdict})
            if verdict != "pass":
                _remember(cfg, p["memory"], "reviewer_finding",
                          "QA %s on task %s" % (verdict, task["id"]),
                          str((qa_out or {}).get("reason", verdict))[:400],
                          task_id=task["id"], cycle_id=run_id, source="qa",
                          importance=0.7)
            if verdict == "pass" and (qa_out or {}).get(
                    "test_integrity_preserved", False):
                break
            review_rounds += 1
            if review_rounds > max_review_rounds:
                # repeated disagreement escalates to the orchestrator: the task
                # blocks with the reviewer's reason; a human decides
                _revert_worktree(worktree)
                log({"event": "review_escalation", "run_id": run_id,
                     "rounds": review_rounds})
                return fail("failure", "QA verdict %s after %d review rounds"
                            % (verdict, review_rounds), block=True,
                            blocking_reason="QA: %s"
                            % str((qa_out or {}).get("reason", verdict))[:200])
            # repair packet: the reviewer's structured findings only — never
            # the reviewer's whole conversation
            feedback = {"failing_checks": [],
                        "qa_findings": (qa_out or {}).get("reason", "qa failed"),
                        "required_repairs": (qa_out or {}).get(
                            "required_repairs") or [],
                        "review_findings": (qa_out or {}).get("findings") or [],
                        "instruction": "address the QA findings within scope"}
            continue
    else:
        log({"event": "parallel_candidates_planned",
             "run_id": run_id, "task_id": task["id"],
             "count": parallel_n, "signals": parallel_signals})
        outcome = parallel_mod.run_candidates(
            cfg, caller, engine, task, order, worker_role, p["root"],
            a, PROJECT_BRANCH, coder_chain, run_id, run_dir, protected,
            authorised_exceptions, worktree, log, load_prompt, _schema,
            _review_input, _apply_and_check_paths)
        if outcome["winner"] is None:
            # item 1/2: classify the aggregate from its STRUCTURED
            # constituent candidate_failures (never by re-parsing
            # `outcome["reasoning"]") -- a platform-only aggregate still
            # blocks (see core.failures' block_with_platform_blocker
            # policy) but is always recoverable (core.parallel_recovery /
            # core.recovery), and a pure provider-capacity aggregate is
            # deferred rather than hard-blocked.
            verdict = parallel_recovery.classify_aggregate(
                outcome.get("candidate_failures") or [])
            return fail(
                "failure",
                "all parallel candidates failed: %s" % outcome["reasoning"],
                block=verdict["block"],
                blocking_reason=(
                    "parallel candidates exhausted: %s"
                    % outcome["reasoning"])[:200] if verdict["block"]
                else None,
                human_only=verdict["human_only"],
                code=(projstate.BLOCKER_CODE_PARALLEL_CANDIDATES_EXHAUSTED
                     if verdict["block"] else None),
                failure_class=verdict["failure_class"],
                platform_owned=verdict["platform_owned"],
                retryable=verdict["retryable"],
                candidate_failures=outcome.get("candidate_failures"))
        # A tied/ambiguous ranking still yields a (deterministically
        # tie-broken) winner that goes on to security review and checks
        # exactly like any other candidate -- ambiguity only ever holds
        # back the final git merge below, never the whole task, and
        # never the security review that evidence should inform.
        integration_requires_human = not outcome["integration_allowed"]
        winner = outcome["winner"]
        worktree = winner["worktree"]
        gate_result = winner["gate_result"]
        qa_out = winner["qa_out"]
        used_backend = winner["backend"]
        integration_task_id = winner["candidate_id"]
        log({"event": "parallel_winner_selected", "run_id": run_id,
             "task_id": task["id"],
             "winner": winner["candidate_id"],
             "integration_requires_human": integration_requires_human,
             "reasoning": outcome["reasoning"]})

    # conditional security review ---------------------------------------------------
    changed = gitops.changed_files(worktree)
    diff = gitops.diff_text(worktree)
    if security_review_required(task, changed, diff):
        sec = caller("security", load_prompt("security-review.md", shared=False),
                     _review_input(order, worktree, gate_result),
                     schema=_schema("security-review.schema.json"),
                     workspace=worktree, permissions="read")
        sec_out = sec["structured_output"] if sec["ok"] else None
        sec_verdict = (sec_out or {}).get("verdict", "uncertain")
        log({"event": "security_review", "run_id": run_id,
             "verdict": sec_verdict})
        if sec_verdict != "pass":
            _remember(cfg, p["memory"], "security_finding",
                      "security %s on task %s" % (sec_verdict, task["id"]),
                      str((sec_out or {}).get("reason", sec_verdict))[:400],
                      task_id=task["id"], cycle_id=run_id,
                      source="security", importance=0.9)
        if sec_verdict == "human_review_required":
            notify.notify(cfg, "security_decision",
                          "Security decision needed",
                          (sec_out or {}).get("reason", "")[:300], p["memory"])
        if sec_verdict != "pass":
            _revert_worktree(worktree)
            return fail("failure", "security review: %s" % sec_verdict,
                        block=True,
                        blocking_reason="security: %s"
                        % (sec_out or {}).get("reason", sec_verdict)[:200])

    # cycle commit + integration into agentic/project ------------------------------
    if looks_like_secret_in_diff(diff):
        _revert_worktree(worktree)
        return fail("failure", "diff appears to contain a secret", block=True,
                    blocking_reason="possible secret in diff")
    if integration_requires_human:
        # candidates tied on every ranking signal: checks and security
        # review already passed above (kept as evidence on the tie-broken
        # winner's own worktree/branch, never deleted), but auto-merging
        # a guess is never acceptable -- a human picks which preserved
        # candidate branch actually becomes agentic/project.
        return fail(
            "failure", "parallel candidate selection ambiguous: %s"
            % outcome["reason"], block=True, human_only=True,
            blocking_reason=("parallel selection ambiguous, merge held "
                             "for human decision: %s"
                             % outcome["reason"])[:200],
            code=projstate.BLOCKER_CODE_GENUINE_HUMAN_DECISION,
            failure_class=failures.GENUINE_HUMAN_DECISION)
    message = "agentic cycle %s: %s (%s)" % (run_id, task["id"],
                                             order["item"][:60])
    gitops.commit_all(worktree, message)
    try:
        taskspace.integrate_task(p["root"], project_worktree, worktree,
                                 integration_task_id, message)
    except errors.PolicyError as exc:
        # dirty target or merge conflict: task worktree kept as evidence
        return fail("failure", "integration failed: %s" % exc.detail,
                    block=True, blocking_reason=exc.detail[:200])
    taskspace.cleanup_task_worktree(p["root"], a, integration_task_id,
                                    success=True)
    _index_project(cfg, project_worktree, p["memory"], log, full=False,
                   changed=changed)
    _record_capability_evidence_safe(a, order, task, gate_result, log,
                                     run_id)
    projstate.update_task(a, task["id"], status="done",
                          attempts=task["attempts"] + 1, last_result="pass",
                          blocking_reason=None)
    complete_detail = "task %s complete" % task["id"]
    if gate_result.get("bootstrap_mode"):
        complete_detail += " (tests: not_configured_yet — structural gate)"
    result = _finish_cycle(cfg, p, scheduler, ledger, log, run_id, task,
                           used_backend, "success", total_tokens, started_at,
                           complete_detail)
    milestone = task.get("milestone")
    progress = result["progress"]
    if milestone and progress["milestones"].get(milestone) == "done":
        notify.notify(cfg, "milestone_complete",
                      "Milestone complete: %s" % milestone,
                      json.dumps(progress["tasks_by_status"]), p["memory"])
    return result


UI_PATH_HINTS = ("ui/", "frontend/", "components/", "styles/", ".css",
                 ".scss", ".tsx", ".jsx", ".vue", ".svelte")


def _worker_role(task, order):
    """UI-shaped tasks route to the ui_designer specialist (role-scoped
    skills and routing); everything else is the coder."""
    kind = str((task or {}).get("kind") or "").lower()
    if kind in ("ui", "frontend", "ui_designer", "design"):
        return "ui_designer"
    paths = " ".join(bootstrap_gate.expected_path_strings(task)
                     + (order or {}).get("allowed_paths", [])).lower()
    if any(hint in paths for hint in UI_PATH_HINTS):
        return "ui_designer"
    return "coder"


def _enrich_work_order_safe(cfg, p, a, order, task, worker_role,
                            capability_plan, ledger, log, run_id):
    """Best-effort Capability-Aware Planning (Phase 10): attaches
    required_capabilities/selected_skills/selected_mcp_tools/
    selected_agent_role/selected_backend/selected_model/
    evidence_requirements/protected_actions to the work order the coder
    receives. A project with no Capability Plan/Graph yet is completely
    unaffected -- and any failure in this NEW machinery is swallowed
    here so it can never break the existing, working cycle loop."""
    if not capability_plan:
        return order
    try:
        from .capability import load_taxonomy
        from .capability.graph import load_graph
        from .capability.predispatch import confirm_ready_for_dispatch
        from .capability.workorder import enrich_work_order
        from . import config as config_mod
        taxonomy = load_taxonomy(strict=False)
        graph = load_graph(a)
        model_registry = None
        try:
            from .modelcap import load_registry
            model_registry = load_registry(p["memory"])
        except Exception:   # noqa: BLE001
            model_registry = None
        enriched = enrich_work_order(
            order, task, graph=graph, taxonomy=taxonomy,
            capability_plan=capability_plan, role=worker_role,
            model_registry=model_registry, ledger=ledger, cfg=cfg)
        protected = gitops.load_protected_paths(cfg,
                                                str(config_mod.AGENTIC_DIR))
        authorised_exceptions = gitops.capability_authorised_exceptions(
            capability_plan)
        ok, warnings = confirm_ready_for_dispatch(
            enriched, graph=graph, model_registry=model_registry,
            protected=protected, authorised_exceptions=authorised_exceptions)
        if not ok:
            log({"event": "predispatch_warnings", "run_id": run_id,
                 "task_id": task.get("id"), "warnings": warnings[:10]})
        return enriched
    except Exception as exc:   # noqa: BLE001
        log({"event": "capability_enrichment_failed", "run_id": run_id,
             "task_id": task.get("id"), "detail": str(exc)[:300]})
        return order


def _record_capability_evidence_safe(a, order, task, gate_result, log,
                                     run_id):
    """Best-effort Capability Graph evidence recording (Phase 10) after a
    task's deterministic checks have already passed. Reuses the
    already-computed `gate_result` as verified evidence -- never a
    model's own claim -- and never able to affect the cycle's outcome:
    any failure here is swallowed and logged."""
    required_ids = order.get("required_capabilities") or []
    if not required_ids:
        return
    try:
        from .capability.graph import load_graph, save_graph
        from .capability.workorder import record_capability_evidence
        graph = load_graph(a)
        if graph is None:
            return
        recorded = record_capability_evidence(
            graph, required_ids, task_id=task.get("id"),
            gate_ok=bool(gate_result and gate_result.get("ok")))
        if recorded:
            save_graph(a, graph)
            log({"event": "capability_evidence_recorded", "run_id": run_id,
                 "task_id": task.get("id"), "capabilities": recorded})
    except Exception as exc:   # noqa: BLE001
        log({"event": "capability_evidence_failed", "run_id": run_id,
             "task_id": task.get("id"), "detail": str(exc)[:300]})


_EMPTY_COMPLETION_CONTRACT = {"requirements": [], "unverified": [],
                              "verified_count": 0, "total_count": 0,
                              "complete": True}


def _build_completion_contract_safe(a, requirements_map, log):
    """Best-effort Completion Contract / Evidence Matrix (Phase 11): a
    project with no requirements_map (or any failure assembling one)
    gets the trivially-complete empty contract -- this must never be
    able to turn an otherwise-complete project into a stuck one, and
    must never fabricate evidence that doesn't exist."""
    if not requirements_map:
        return dict(_EMPTY_COMPLETION_CONTRACT)
    try:
        from . import completion
        from .capability.graph import load_graph
        backlog = projstate.load_backlog(a)
        graph = load_graph(a)
        return completion.build_completion_contract(requirements_map,
                                                     backlog, graph=graph)
    except Exception as exc:   # noqa: BLE001
        log({"event": "completion_contract_failed",
             "detail": str(exc)[:300]})
        return dict(_EMPTY_COMPLETION_CONTRACT)


def _failure_fingerprint(failing, worktree):
    """Stable fingerprint of (what failed, what the diff was). An identical
    fingerprint means retrying is guaranteed to waste budget."""
    import hashlib
    basis = "\n".join(sorted("%s|%s" % (r["name"], r["detail"][:200])
                             for r in failing))
    basis += "\n===diff===\n" + gitops.diff_text(worktree)
    return hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()[:16]


def _invoke_coder(cfg, caller, coder_input, worktree, chain, role="coder"):
    """CLI backends edit the worktree directly; API/local backends return
    structured edits which we apply. Both paths produce a diffed worktree."""
    primary_type = (cfg.get("backends") or {}).get(chain[0], {}).get("type",
                                                                     "api")
    if primary_type == "cli":
        result = caller(role, load_prompt("coder-cli.md", shared=False),
                        coder_input,
                        schema=None, workspace=worktree, permissions="write",
                        chain=chain)
        if result["ok"]:
            content = result.get("content", "")
            if content.strip().startswith("BLOCKED:"):
                result["blocked"] = True
                result["blocker"] = content.strip()[8:250].strip()
            result["edits"] = None   # CLI edited files itself
        return result
    result = caller(role, load_prompt("implement.md", shared=False),
                    coder_input,
                    schema=_schema("worker.schema.json"), workspace=worktree,
                    permissions="write", chain=chain)
    if result["ok"]:
        out = result["structured_output"]
        result["blocked"] = out.get("blocked", False)
        result["blocker"] = out.get("blocker")
        result["edits"] = out.get("edits", [])
    return result


def _persist_evidence(run_dir, name, data):
    """Best-effort structured evidence persistence (item 4) -- redacted
    before it ever touches disk, exactly like every other audit trail
    entry. Never raises: capturing evidence must never break a cycle."""
    try:
        text = redact(json.dumps(data, indent=2, default=str))
        with open(os.path.join(run_dir, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:   # noqa: BLE001
        pass


def _apply_and_check_paths(cfg, result, order, worktree, protected,
                           authorised_exceptions=None, log=None,
                           run_dir=None, attempt=None):
    if result.get("edits") is not None:
        violations = apply_edits(worktree, result["edits"],
                                 order["allowed_paths"],
                                 order.get("forbidden_paths", []), protected,
                                 authorised_exceptions=authorised_exceptions,
                                 log=log)
    else:
        violations = []
    gitops.stage_all(worktree)
    # tool-generated artefacts (__pycache__, .pytest_cache, node_modules/
    # caches) are a side effect of running an EARLIER attempt's
    # deterministic checks in this same reused worktree, never something
    # the worker chose to write -- excluded before the scope-violation
    # check ever sees them, or a passing repair attempt could be wrongly
    # blocked by the previous attempt's own check output.
    files = gitops.filter_tool_artifacts(gitops.changed_files(worktree))
    violations += gitops.check_paths(files, order["allowed_paths"],
                                     order.get("forbidden_paths", []),
                                     protected,
                                     authorised_exceptions=authorised_exceptions)
    lines = gitops.changed_lines(worktree)
    limit = min(int(order.get("maximum_changed_lines") or 0) or 10 ** 9,
                int(cfg.get("execution", {}).get("max_changed_lines", 400)))
    if lines > limit:
        violations.append("changed lines %d exceed limit %d" % (lines, limit))
    violations = sorted(set(violations))
    if run_dir:
        _persist_evidence(
            run_dir, "applied-edits-%s.json" % (attempt or "0"),
            {"edits": result.get("edits"), "violations": violations,
             "changed_files": files, "changed_lines": lines})
    return violations


def _revert_worktree(worktree):
    gitops.run_git(["reset", "--hard", "HEAD"], cwd=worktree, check=False)
    gitops.run_git(["clean", "-fd"], cwd=worktree, check=False)


def _review_input(order, worktree, gate_result, task=None):
    """The reviewer's fresh context: order, acceptance criteria, diff, and
    deterministic evidence — never the worker's conversation."""
    return {"work_order": order,
            "acceptance_criteria": (task or {}).get("acceptance_criteria",
                                                    []),
            "changed_files": gitops.changed_files(worktree),
            "diff": redact(gitops.diff_text(worktree)),
            "deterministic_checks": {
                "ok": gate_result["ok"],
                "tests": gate_result.get("tests", "not_configured_yet"),
                "results": [
                    {"name": r["name"],
                     "passed": r["passed"],
                     "mandatory": r["mandatory"],
                     "command": r.get("command"),
                     # Preserve bounded semantic evidence (for example named
                     # subtests) so QA can judge coverage instead of seeing
                     # only a boolean. Keep the cap small enough that a noisy
                     # suite cannot consume the reviewer context budget.
                     "detail": (r.get("detail") or "")[:1200]}
                    for r in gate_result["results"]]}}


def _handoff_payload(order, worktree, gate_result, remaining_chain):
    """Structured handoff for a fallback coder: original order, current diff,
    failing checks, remaining criteria, allowed paths — nothing else."""
    return {"handoff": True, "handoff_chain": remaining_chain,
            "original_work_order": order,
            "current_diff": redact(gitops.diff_text(worktree)),
            "failing_checks": [
                {"name": r["name"], "detail": r["detail"][:400]}
                for r in (gate_result or {}).get("results", [])
                if r.get("mandatory") and not r.get("passed")],
            "allowed_paths": order["allowed_paths"],
            "remaining_criteria": [d["condition"]
                                   for d in order.get("done_when", [])]}


# -- run / resume / status / pause ---------------------------------------------------

def project_run(cfg, caller=None, overrides=None, max_cycles=1, clock=None,
                **kw):
    """Run up to max_cycles eligible cycles, then return. Long waits are
    persisted (scheduler.next_run_at), never slept through — re-invoke (or
    let a timer re-invoke) to continue."""
    results = []
    for _ in range(max(1, int(max_cycles))):
        result = run_cycle(cfg, caller=caller, overrides=overrides,
                           clock=clock, **kw)
        results.append(result)
        if result["status"] != "success":
            break
        continuation = ((cfg.get("scheduler") or {}).get("continuation")
                        or {})
        if not continuation.get("automatic", True):
            break
    return results[-1] if len(results) == 1 else {"status": "multi",
                                                  "cycles": results}


def project_status(cfg):
    p = _paths(cfg)
    a = p["agentic"]
    scheduler = Scheduler(cfg, p["memory"])
    progress = projstate.read_yaml(a, "progress.yaml", {}) or {}
    return {"scheduler": scheduler.state, "progress": progress,
            "blockers": projstate.open_blockers(a),
            "project_exists": projstate.exists(a)}


def project_pause(cfg):
    p = _paths(cfg)
    Scheduler(cfg, p["memory"]).pause()
    return {"status": "paused"}


def project_resume(cfg):
    p = _paths(cfg)
    scheduler = Scheduler(cfg, p["memory"])
    scheduler.resume()
    return {"status": scheduler.state["state"],
            "next_run_at": scheduler.state.get("next_run_at")}


# -- final audit --------------------------------------------------------------------------


def _historical_task_evidence(runs_dir, limit=20):
    """Collect bounded, successful canonical task-check evidence for final QA.

    The newest successful validation per task wins. Only task-specific checks
    are included, preventing auto-detected suite noise from consuming the
    final review context.
    """
    evidence = {}
    try:
        cycle_names = sorted(
            (name for name in os.listdir(runs_dir)
             if name.startswith("cycle-")), reverse=True)
    except OSError:
        return []
    for cycle_name in cycle_names:
        cycle_dir = os.path.join(runs_dir, cycle_name)
        contract_path = os.path.join(cycle_dir, "task-contract.json")
        try:
            with open(contract_path, encoding="utf-8") as fh:
                contract = json.load(fh)
        except (OSError, ValueError):
            continue
        task_id = contract.get("task_id")
        if not task_id or task_id in evidence:
            continue
        required = set(str(command) for command in
                       contract.get("deterministic_checks") or [])
        try:
            validation_names = sorted(
                (name for name in os.listdir(cycle_dir)
                 if name.startswith("validation-result-") and
                 name.endswith(".json")), reverse=True)
        except OSError:
            continue
        for validation_name in validation_names:
            try:
                with open(os.path.join(cycle_dir, validation_name),
                          encoding="utf-8") as fh:
                    validation = json.load(fh)
            except (OSError, ValueError):
                continue
            if not validation.get("ok"):
                continue
            checks = []
            for result in validation.get("results") or []:
                command = str(result.get("command") or "")
                if not result.get("passed") or not result.get("mandatory"):
                    continue
                if not (str(result.get("name") or "").startswith(
                        "task-deterministic-") or command in required):
                    continue
                checks.append({
                    "name": result.get("name"),
                    "command": command,
                    "passed": True,
                    "detail": (str(result.get("detail") or ""))[-1200:],
                })
            if checks:
                evidence[task_id] = {
                    "task_id": task_id,
                    "run_id": contract.get("run_id") or
                              cycle_name.replace("cycle-", "", 1),
                    "acceptance_criteria":
                        contract.get("acceptance_criteria") or [],
                    "checks": checks[:2],
                }
                break
        if len(evidence) >= limit:
            break
    return [evidence[key] for key in sorted(evidence)]

def final_audit(cfg, caller=None, overrides=None, clock=None,
                _preloaded=None, **kw):
    """Completion requires evidence, not an empty backlog."""
    p = _paths(cfg)
    a = p["agentic"]
    if _preloaded:
        ledger, board, scheduler, log = _preloaded
        caller = caller
    else:
        ledger, board, scheduler, caller, log = _context(
            cfg, p["memory"], overrides=overrides, caller=caller,
            clock=clock, **kw)
    worktree = ensure_project_worktree(cfg, p)
    progress = projstate.refresh_progress(a)
    criteria = projstate.read_yaml(a, "acceptance-criteria.yaml", {}) or {}
    try:
        with open(os.path.join(projstate.project_dir(a), "PROJECT.md"),
                  encoding="utf-8") as fh:
            source_plan = fh.read()[-6000:]
    except OSError:
        source_plan = ""
    checks = {}
    checks["backlog_complete"] = progress.get("backlog_complete", False)
    checks["all_milestones_done"] = bool(progress.get("milestones")) and all(
        s == "done" for s in progress["milestones"].values())
    checks["no_open_blockers"] = not projstate.open_blockers(a)
    gate_result = gate.run_checks(cfg, worktree,
                                  os.path.join(p["runs"], "final-audit"))
    completion_criteria = criteria.get("completion_criteria", [])
    needs_local_browser_smoke = any(
        "browser" in str(item).lower() and
        ("open" in str(item).lower() or "load" in str(item).lower())
        for item in completion_criteria)
    if needs_local_browser_smoke:
        browser_smoke = gate.run_local_static_app_smoke(worktree)
        gate_result["results"].append(browser_smoke)
        if browser_smoke["mandatory"] and not browser_smoke["passed"]:
            gate_result["ok"] = False
        checks["local_browser_smoke"] = browser_smoke["passed"]
    checks["deterministic_checks_pass"] = gate_result["ok"] and \
        not gate_result["no_checks"]
    # running the deterministic checks just above is itself what can
    # leave __pycache__/.pytest_cache/etc behind in the worktree -- never
    # a real "uncommitted change" the audit should fail on.
    status_lines = gitops.run_git(["status", "--porcelain"], cwd=worktree,
                                  check=False).strip().splitlines()
    dirty_paths = [line[3:].strip() for line in status_lines if line]
    dirty_paths = gitops.filter_tool_artifacts(dirty_paths)
    checks["no_uncommitted_changes"] = not dirty_paths
    diff_all = gitops.run_git(["log", "-p", "--max-count=50",
                               PROJECT_BRANCH, "--", "."],
                              cwd=worktree, check=False)
    checks["no_committed_secrets"] = not looks_like_secret_in_diff(diff_all)
    checks["env_example_present"] = (
        not _needs_env(worktree) or
        os.path.exists(os.path.join(worktree, ".env.example")))
    completion_contract = _build_completion_contract_safe(
        a, criteria.get("requirements_map", []), log)
    checks["completion_contract_verified"] = completion_contract["complete"]
    task_evidence = _historical_task_evidence(p["runs"])
    review = None
    if all(checks.values()) and caller is not None:
        # the final auditor gets its own routing chain when the capability
        # router configures one; the review contract itself is the QA one
        try:
            auditor_chain = backends.routing_chain(
                cfg, "final_auditor", overrides,
                memory_dir=p["memory"], board=board, ledger=ledger) \
                if (cfg.get("routing") or {}).get("mode") == "capability" \
                else None
        except errors.AgenticError:
            auditor_chain = None
        final = caller("qa", load_prompt("qa-review.md", shared=False),
                       {"work_order": {"item": "final project audit",
                                       "done_when": [
                                           {"id": "C-%d" % i, "condition": c}
                                           for i, c in enumerate(
                                               criteria.get(
                                                   "completion_criteria", []))],
                                       "allowed_paths": ["**"],
                                       "spec": "independent final review"},
                        "progress": progress,
                        "source_plan": source_plan,
                        "completion_contract": completion_contract,
                        "historical_task_evidence": task_evidence,
                        "deterministic_checks": {
                            "ok": gate_result["ok"],
                            "results": [{
                                "name": r["name"],
                                "passed": r["passed"],
                                "mandatory": r["mandatory"],
                                "command": r.get("command"),
                                "detail": (r.get("detail") or "")[-1200:],
                            } for r in gate_result["results"]]},
                        "diff": "final audit: live project state is authoritative",
                        "changed_files": []},
                       schema=_schema("verification.schema.json"),
                       workspace=worktree, permissions="read",
                       chain=auditor_chain)
        review = final["structured_output"] if final["ok"] else None
        checks["final_independent_review"] = bool(
            review and review.get("verdict") == "pass")
    else:
        checks["final_independent_review"] = False
    complete = all(checks.values())
    audit = {"completed_at": _dt.datetime.now().isoformat(timespec="seconds"),
             "complete": complete, "checks": checks,
             "final_review": review,
             "completion_criteria": criteria.get("completion_criteria", []),
             "source_plan": source_plan,
             "completion_contract": completion_contract,
             "historical_task_evidence": task_evidence,
             "branch": PROJECT_BRANCH}
    projstate.write_yaml(a, "final-audit.yaml", audit)
    from .knowledge import update_knowledge
    update_knowledge(cfg, a, log)
    if complete:
        scheduler.mark_complete()
        _unload_local_models_safe(cfg, log)
        notify.notify(cfg, "project_complete", "Application ready for review",
                      "All audits passed. Review branch %s and merge when "
                      "satisfied." % PROJECT_BRANCH, p["memory"])
        return {"status": "complete", "audit": audit}
    scheduler.set_project_status("audit_failed")
    return {"status": "audit_failed",
            "failed_checks": [k for k, v in checks.items() if not v]}


def _unload_local_models_safe(cfg, log):
    """Best-effort: release any configured local (Ollama) model's memory
    once a project completes -- one of the documented unload triggers
    (project completes / memory pressure / another local model needed /
    user request). Never able to affect the completion result itself."""
    for name, bcfg in (cfg.get("backends") or {}).items():
        if (bcfg or {}).get("type") != "local":
            continue
        try:
            adapter = backends.build_backend(cfg, name)
            if hasattr(adapter, "unload"):
                result = adapter.unload("project_complete")
                log({"event": "local_model_unloaded", "backend": name,
                     "ok": result.get("ok")})
        except Exception as exc:   # noqa: BLE001
            log({"event": "local_model_unload_failed", "backend": name,
                 "detail": str(exc)[:200]})


def _needs_env(worktree):
    for name in gitops.run_git(["ls-files"], cwd=worktree,
                               check=False).splitlines():
        if name.endswith((".py", ".js", ".ts")):
            try:
                with open(os.path.join(worktree, name), encoding="utf-8",
                          errors="replace") as fh:
                    content = fh.read()
                if "os.environ" in content or "process.env" in content:
                    return True
            except OSError:
                pass
    return False
