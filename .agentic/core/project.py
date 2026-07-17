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

from . import backends, capacity as capacity_mod, config as config_mod
from . import errors, gate, gitops, logs, notify, projstate
from .breaker import BreakerBoard
from .orchestrator import (apply_edits, load_prompt, _schema, _snapshot)
from .redact import looks_like_secret, redact
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

    def call(role, prompt, input_data=None, schema=None, workspace=None,
             permissions="read", timeout=None, chain=None):
        chain = chain or backends.routing_chain(cfg, role, overrides)
        try:
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
                              memory_dir=memory_dir, backend=chain[0],
                              extra_items=retrieved)
        except BrokerError as exc:
            err = errors.PolicyError("context broker: %s" % exc)
            (log or (lambda e: None))({"event": "context_budget_stop",
                                       "role": role,
                                       "detail": str(exc)[:300]})
            return backends.error_result(chain[0], role, err)
        return backends.invoke_backend(
            cfg, chain[0], role, package.rendered, input_data=None,
            output_schema=schema, workspace=workspace,
            permissions=permissions, timeout=timeout, ledger=ledger,
            board=board, fallback_chain=chain[1:], runner=runner,
            transport=transport, which=which, env=env, log=log)
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
    snapshot = _snapshot(p["root"], ["**"])
    result = caller("architect", load_prompt("architect.md", shared=False),
                    {"plan": plan, "repository_files": snapshot["file_list"]},
                    schema=_schema("architect.schema.json"),
                    workspace=p["root"], permissions="read")
    if not result["ok"]:
        return {"status": "architect_failed",
                "error": (result.get("error") or {}).get("kind")}
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
    for decision in out.get("human_decisions", []):
        projstate.add_blocker(a, None, decision, human_only=True)
    return {"status": "started", "tasks": len(tasks),
            "milestones": len(out["milestones"]),
            "human_decisions": out.get("human_decisions", [])}


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
    ok, reason = scheduler.eligible()
    if not ok:
        return {"status": "not_eligible", "reason": reason,
                "next_run_at": scheduler.state.get("next_run_at")}

    lock = projstate.ProjectLock(a)
    if not lock.acquire():
        return {"status": "locked", "detail": "another cycle is running"}
    try:
        return _run_cycle_locked(cfg, p, ledger, board, scheduler, caller,
                                 log, overrides, run_id)
    finally:
        lock.release()


def _finish_cycle(cfg, p, scheduler, ledger, log, run_id, task, backend,
                  outcome, tokens, started_at, detail="", retry_after=None):
    duration = int((_dt.datetime.now() - started_at).total_seconds())
    ledger.record_cycle(run_id, backend or "-",
                        (task or {}).get("skill") or (task or {}).get("id", "-"),
                        (task or {}).get("expected_size", "medium"),
                        tokens, duration, outcome)
    cool_outcome = outcome if outcome in ("success", "rate_limit",
                                          "usage_limit") else "failure"
    until = scheduler.start_cooling(cool_outcome,
                                    retry_after_seconds=retry_after)
    _remember(cfg, p["memory"], "cycle_outcome",
              "cycle %s: %s" % (run_id, outcome),
              (detail or outcome)[:400], task_id=(task or {}).get("id"),
              cycle_id=run_id, source="cycle",
              importance=0.6 if outcome == "success" else 0.7)
    log({"event": "cycle_finished", "run_id": run_id, "outcome": outcome,
         "detail": redact(str(detail))[:300],
         "cooling_until": until.isoformat(timespec="seconds")})
    progress = projstate.refresh_progress(p["agentic"])
    from .knowledge import update_knowledge
    update_knowledge(cfg, p["agentic"], log)
    result = {"status": outcome, "run_id": run_id,
              "task": (task or {}).get("id"), "detail": detail,
              "cooling_until": until.isoformat(timespec="seconds"),
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
    started_at = _dt.datetime.now()
    run_dir = os.path.join(p["runs"], "cycle-" + run_id)
    os.makedirs(run_dir, exist_ok=True)
    protected = gitops.load_protected_paths(cfg, a)

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
    chain = backends.routing_chain(cfg, "coder", overrides)
    decision = capacity_mod.decide_start(cfg, task, ledger, board, chain)
    log({"event": "capacity_decision", **decision})
    if decision["decision"] == "wait":
        until = decision.get("wait_until")
        scheduler.state.update(state="cooling", cooling_reason="capacity",
                               next_run_at=until)
        scheduler.save()
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
             blocking_reason=None):
        projstate.update_task(
            a, task["id"],
            status="blocked" if block else "pending",
            attempts=task["attempts"] + 1, last_result=outcome,
            blocking_reason=blocking_reason or (detail[:200] if block else None))
        if block:
            projstate.add_blocker(a, task["id"], blocking_reason or detail)
            _remember(cfg, p["memory"], "failed_attempt",
                      "task %s blocked" % task["id"],
                      (blocking_reason or detail)[:400],
                      task_id=task["id"], cycle_id=run_id,
                      source="cycle", importance=0.8)
        return _finish_cycle(cfg, p, scheduler, ledger, log, run_id, task,
                             backend, outcome, 0, started_at, detail,
                             retry_after)

    # conductor -------------------------------------------------------------------
    worktree = ensure_project_worktree(cfg, p)
    conducted = caller(
        "conductor", load_prompt("project-conductor.md", shared=False),
        {"task": task,
         "architecture": (projstate.read_yaml(a, "progress.yaml", {}) or {}),
         "repository_files": _snapshot(worktree, ["**"])["file_list"][:300],
         "limits": {"max_changed_lines":
                    cfg.get("execution", {}).get("max_changed_lines", 400)}},
        schema=_schema("work-order.schema.json"), workspace=worktree,
        permissions="read")
    if not conducted["ok"]:
        kind = (conducted.get("error") or {}).get("kind", "?")
        retry = (conducted.get("capacity") or {}).get("retry_after_seconds")
        return fail(kind if kind in ("rate_limit", "usage_limit")
                    else "failure", "conductor failed: %s" % kind, retry)
    order = conducted["structured_output"]
    with open(os.path.join(run_dir, "work-order.json"), "w",
              encoding="utf-8") as fh:
        json.dump(order, fh, indent=2)
    if order["action"] == "queue":
        return fail("failure", "conductor queued: %s" % order.get("queue_reason"),
                    block=True, blocking_reason=order.get("queue_reason"))
    for pattern in order.get("allowed_paths", []):
        if gitops.matches_any(pattern, protected) or \
                any(gitops.match_pattern(pp, pattern) for pp in protected):
            return fail("failure", "work order grants protected path %s"
                        % pattern, block=True,
                        blocking_reason="protected path in work order")

    # coder + deterministic checks + repair loop -------------------------------------
    repair_cfg = cfg.get("repair") or {}
    max_attempts = int(repair_cfg.get("maximum_attempts_per_task", 3))
    gate_result = None
    qa_out = None
    attempt = 0
    feedback = None
    used_backend = backend
    total_tokens = 0
    while attempt < max_attempts:
        attempt += 1
        coder_input = {"work_order": order}
        if feedback:
            coder_input.update(feedback)   # structured repair/handoff payload
        chain_now = coder_chain
        if feedback and feedback.get("handoff"):
            chain_now = feedback["handoff_chain"]
        result = _invoke_coder(cfg, caller, coder_input, worktree, chain_now)
        if not result["ok"]:
            kind = (result.get("error") or {}).get("kind", "?")
            retry = (result.get("capacity") or {}).get("retry_after_seconds")
            if kind in ("rate_limit", "usage_limit") and attempt < max_attempts:
                # backend down mid-task: structured handoff to next backend
                remaining = [b for b in coder_chain
                             if b != result.get("backend")]
                if remaining:
                    feedback = _handoff_payload(order, worktree, gate_result,
                                                remaining)
                    used_backend = remaining[0]
                    log({"event": "handoff", "run_id": run_id,
                         "from": result.get("backend"), "to": remaining[0]})
                    continue
            return fail(kind if kind in ("rate_limit", "usage_limit")
                        else "failure", "coder failed: %s" % kind, retry)
        used_backend = result.get("backend", used_backend)
        usage = result.get("usage") or {}
        total_tokens += (usage.get("input_tokens") or 0) + \
                        (usage.get("output_tokens") or 0)
        if result.get("blocked"):
            return fail("failure", result.get("blocker") or "coder blocked",
                        block=True, blocking_reason=result.get("blocker"))

        violations = _apply_and_check_paths(cfg, result, order, worktree,
                                            protected)
        if violations:
            feedback = {"failing_checks": [],
                        "scope_violations": violations,
                        "instruction": "revert or move out-of-scope changes"}
            log({"event": "scope_violation", "run_id": run_id,
                 "violations": violations[:5]})
            if attempt >= max_attempts:
                _revert_worktree(worktree)
                return fail("failure", "scope violations: %s"
                            % "; ".join(violations[:3]), block=True,
                            blocking_reason="repeated scope violations")
            continue

        gate_result = gate.run_checks(cfg, worktree,
                                      os.path.join(run_dir,
                                                   "checks-%d" % attempt))
        if gate_result["no_checks"]:
            _revert_worktree(worktree)
            projstate.add_blocker(a, task["id"],
                                  "no deterministic checks configured; "
                                  "configure verification.commands",
                                  human_only=True)
            return fail("failure", "zero deterministic checks: blocking",
                        block=True,
                        blocking_reason="no deterministic checks configured")
        if not gate_result["ok"]:
            failing = [r for r in gate_result["results"]
                       if r["mandatory"] and not r["passed"]]
            feedback = {"failing_checks":
                        [{"name": r["name"], "detail": r["detail"][:400]}
                         for r in failing],
                        "instruction": "make the failing checks pass; do not "
                                       "weaken or delete tests"}
            log({"event": "gate_failed", "run_id": run_id, "attempt": attempt,
                 "failing": [r["name"] for r in failing]})
            continue

        # QA review (independent, fresh context) --------------------------------
        qa_input = _review_input(order, worktree, gate_result)
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
        feedback = {"failing_checks": [],
                    "qa_findings": (qa_out or {}).get("reason", "qa failed"),
                    "instruction": "address the QA findings within scope"}
        if attempt >= max_attempts:
            _revert_worktree(worktree)
            return fail("failure", "QA verdict %s after %d attempts"
                        % (verdict, attempt), block=True,
                        blocking_reason="QA: %s"
                        % (qa_out or {}).get("reason", verdict)[:200])
    else:
        _revert_worktree(worktree)
        return fail("failure", "deterministic checks failing after %d attempts"
                    % max_attempts, block=True,
                    blocking_reason="repair attempts exhausted")

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

    # cycle commit + state update ------------------------------------------------------
    if looks_like_secret(diff):
        _revert_worktree(worktree)
        return fail("failure", "diff appears to contain a secret", block=True,
                    blocking_reason="possible secret in diff")
    gitops.commit_all(worktree, "agentic cycle %s: %s (%s)"
                      % (run_id, task["id"], order["item"][:60]))
    _index_project(cfg, worktree, p["memory"], log, full=False,
                   changed=changed)
    projstate.update_task(a, task["id"], status="done",
                          attempts=task["attempts"] + 1, last_result="pass",
                          blocking_reason=None)
    result = _finish_cycle(cfg, p, scheduler, ledger, log, run_id, task,
                           used_backend, "success", total_tokens, started_at,
                           "task %s complete" % task["id"])
    milestone = task.get("milestone")
    progress = result["progress"]
    if milestone and progress["milestones"].get(milestone) == "done":
        notify.notify(cfg, "milestone_complete",
                      "Milestone complete: %s" % milestone,
                      json.dumps(progress["tasks_by_status"]), p["memory"])
    return result


def _invoke_coder(cfg, caller, coder_input, worktree, chain):
    """CLI backends edit the worktree directly; API/local backends return
    structured edits which we apply. Both paths produce a diffed worktree."""
    primary_type = (cfg.get("backends") or {}).get(chain[0], {}).get("type",
                                                                     "api")
    if primary_type == "cli":
        result = caller("coder", load_prompt("coder-cli.md", shared=False), coder_input,
                        schema=None, workspace=worktree, permissions="write",
                        chain=chain)
        if result["ok"]:
            content = result.get("content", "")
            if content.strip().startswith("BLOCKED:"):
                result["blocked"] = True
                result["blocker"] = content.strip()[8:250].strip()
            result["edits"] = None   # CLI edited files itself
        return result
    result = caller("coder", load_prompt("implement.md", shared=False), coder_input,
                    schema=_schema("worker.schema.json"), workspace=worktree,
                    permissions="write", chain=chain)
    if result["ok"]:
        out = result["structured_output"]
        result["blocked"] = out.get("blocked", False)
        result["blocker"] = out.get("blocker")
        result["edits"] = out.get("edits", [])
    return result


def _apply_and_check_paths(cfg, result, order, worktree, protected):
    if result.get("edits") is not None:
        violations = apply_edits(worktree, result["edits"],
                                 order["allowed_paths"],
                                 order.get("forbidden_paths", []), protected)
    else:
        violations = []
    gitops.stage_all(worktree)
    files = gitops.changed_files(worktree)
    violations += gitops.check_paths(files, order["allowed_paths"],
                                     order.get("forbidden_paths", []),
                                     protected)
    lines = gitops.changed_lines(worktree)
    limit = min(int(order.get("maximum_changed_lines") or 0) or 10 ** 9,
                int(cfg.get("execution", {}).get("max_changed_lines", 400)))
    if lines > limit:
        violations.append("changed lines %d exceed limit %d" % (lines, limit))
    return sorted(set(violations))


def _revert_worktree(worktree):
    gitops.run_git(["reset", "--hard", "HEAD"], cwd=worktree, check=False)
    gitops.run_git(["clean", "-fd"], cwd=worktree, check=False)


def _review_input(order, worktree, gate_result):
    return {"work_order": order,
            "changed_files": gitops.changed_files(worktree),
            "diff": redact(gitops.diff_text(worktree)),
            "deterministic_checks": {
                "ok": gate_result["ok"],
                "results": [{k: r[k] for k in ("name", "passed", "mandatory")}
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
    checks = {}
    checks["backlog_complete"] = progress.get("backlog_complete", False)
    checks["all_milestones_done"] = bool(progress.get("milestones")) and all(
        s == "done" for s in progress["milestones"].values())
    checks["no_open_blockers"] = not projstate.open_blockers(a)
    gate_result = gate.run_checks(cfg, worktree,
                                  os.path.join(p["runs"], "final-audit"))
    checks["deterministic_checks_pass"] = gate_result["ok"] and \
        not gate_result["no_checks"]
    status = gitops.run_git(["status", "--porcelain"], cwd=worktree,
                            check=False).strip()
    checks["no_uncommitted_changes"] = status == ""
    diff_all = gitops.run_git(["log", "-p", "--max-count=50",
                               PROJECT_BRANCH, "--", "."],
                              cwd=worktree, check=False)
    checks["no_committed_secrets"] = not looks_like_secret(diff_all)
    checks["env_example_present"] = (
        not _needs_env(worktree) or
        os.path.exists(os.path.join(worktree, ".env.example")))
    review = None
    if all(checks.values()) and caller is not None:
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
                        "deterministic_checks": {
                            "ok": gate_result["ok"],
                            "results": [
                                {k: r[k] for k in ("name", "passed",
                                                   "mandatory")}
                                for r in gate_result["results"]]},
                        "diff": "final audit: see repository state",
                        "changed_files": []},
                       schema=_schema("verification.schema.json"),
                       workspace=worktree, permissions="read")
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
             "branch": PROJECT_BRANCH}
    projstate.write_yaml(a, "final-audit.yaml", audit)
    from .knowledge import update_knowledge
    update_knowledge(cfg, a, log)
    if complete:
        scheduler.mark_complete()
        notify.notify(cfg, "project_complete", "Application ready for review",
                      "All audits passed. Review branch %s and merge when "
                      "satisfied." % PROJECT_BRANCH, p["memory"])
        return {"status": "complete", "audit": audit}
    scheduler.set_project_status("audit_failed")
    return {"status": "audit_failed",
            "failed_checks": [k for k, v in checks.items() if not v]}


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
