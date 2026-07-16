"""Main workflow: triage -> conductor -> worker -> independent verifier ->
deterministic gate -> trust policy -> draft | queue | approved action.

Every policy decision here is code, not prompt: path rules, line limits,
budget stops, trust tiers, and the deterministic gate cannot be talked out
of by any model or by text embedded in repository content."""
import datetime as _dt
import json
import os
import shutil
import subprocess

from . import config as config_mod
from . import errors, execpolicy, gate, gitops, goals, logs, queueing
from .budget import Budget
from .config import load_config, repo_root
from .invoke import invoke_model
from .redact import looks_like_secret, redact
from .schema import load_schema
from .trust import TIER_AUTO, TrustLedger

DEPENDENCY_MANIFESTS = [
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "requirements*.txt", "Pipfile", "Pipfile.lock", "poetry.lock",
    "pyproject.toml", "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
    "Gemfile", "Gemfile.lock", "composer.json",
]

MAX_SNAPSHOT_FILES = 20
MAX_SNAPSHOT_BYTES = 200_000


def new_run_id():
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _paths(cfg):
    a = str(config_mod.AGENTIC_DIR)
    return {"agentic": a, "memory": os.path.join(a, "memory"),
            "queue": os.path.join(a, "queue"), "runs": os.path.join(a, "runs"),
            "root": str(repo_root(cfg))}


def load_prompt(name, shared=True):
    base = config_mod.AGENTIC_DIR / "prompts"
    parts = []
    if shared:
        for s in ("shared-autonomy.md", "shared-scope.md"):
            parts.append((base / s).read_text(encoding="utf-8"))
    parts.append((base / name).read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def _schema(name):
    return load_schema(str(config_mod.AGENTIC_DIR / "schemas" / name))


def _gh(args, cwd):
    if not shutil.which("gh"):
        return None
    try:
        proc = subprocess.run(["gh"] + args, cwd=cwd, capture_output=True,
                              text=True, timeout=30)
        return proc.stdout.strip() if proc.returncode == 0 else None
    except Exception:
        return None


def gather_context(cfg, p):
    root = p["root"]
    ctx = {"repository": cfg.get("project", {}).get("name", "?")}
    try:
        ctx["recent_commits"] = gitops.run_git(
            ["log", "--oneline", "-15"], cwd=root, check=False).strip()
        ctx["status"] = gitops.run_git(
            ["status", "--porcelain"], cwd=root, check=False).strip()[:4000]
    except Exception as exc:
        ctx["git_error"] = str(exc)
    if (cfg.get("integrations", {}) or {}).get("github_cli", "auto") != "off":
        issues = _gh(["issue", "list", "--limit", "10",
                      "--json", "number,title,labels"], cwd=root)
        ci = _gh(["run", "list", "--limit", "5",
                  "--json", "displayTitle,conclusion,status"], cwd=root)
        if issues:
            ctx["open_issues"] = issues[:4000]
        if ci:
            ctx["ci_results"] = ci[:4000]
    violations, _results = goals.check_goals(cfg, p["agentic"], root)
    ctx["goal_violations"] = [
        {"id": v["id"], "detail": v["detail"]} for v in violations]
    return ctx, violations


# -- policy gate (pure, code-enforced) ---------------------------------------

def apply_policy(cfg, order, trust_ledger, protected_patterns):
    """Decide what a validated work order is allowed to do. This is the
    prompt-injection boundary: no matter what text talked the conductor into,
    these checks cannot be overridden by content."""
    reasons = []
    execution = cfg.get("execution", {}) or {}
    if order["action"] == "stop":
        return {"action": "stop", "reasons": ["conductor chose stop"]}
    if order["action"] == "queue":
        return {"action": "queue",
                "reasons": [order.get("queue_reason") or "conductor queued"]}
    if order.get("risk") == "high":
        reasons.append("risk marked high")
    if not order.get("done_when"):
        reasons.append("no machine-verifiable done_when conditions")
    if not order.get("allowed_paths"):
        reasons.append("empty allowed_paths (deny by default)")
    max_lines = int(execution.get("max_changed_lines", 400))
    if int(order.get("maximum_changed_lines", 0)) > max_lines:
        reasons.append("maximum_changed_lines %s exceeds configured limit %s"
                       % (order["maximum_changed_lines"], max_lines))
    for pattern in order.get("allowed_paths", []):
        if gitops.matches_any(pattern, protected_patterns) or \
                any(gitops.match_pattern(pp, pattern) for pp in protected_patterns):
            reasons.append("allowed_paths would grant protected path: %s" % pattern)
        if gitops.matches_any(pattern, DEPENDENCY_MANIFESTS):
            reasons.append("dependency manifest requires approval: %s" % pattern)
    if trust_ledger.is_sensitive(order.get("skill", "")):
        reasons.append("skill is contract-sensitive")
    if looks_like_secret(json.dumps(order)):
        reasons.append("work order appears to contain a secret")
    if reasons:
        return {"action": "queue", "reasons": reasons}
    return {"action": "execute", "reasons": []}


# -- worker edit application (code-enforced) ---------------------------------

def apply_edits(worktree, edits, allowed, forbidden, protected):
    """Apply worker edits with hard path enforcement. Returns violations;
    on any violation nothing more is applied."""
    violations = []
    for edit in edits:
        rel = edit.get("path", "")
        bad = gitops.check_paths([rel], allowed, forbidden, protected)
        if bad:
            violations.extend(bad)
            continue
        try:
            full = gitops.safe_join(worktree, rel)
        except errors.PolicyError as exc:
            violations.append(exc.detail)
            continue
        if edit.get("action") == "delete":
            if os.path.exists(full):
                os.remove(full)
        else:
            if looks_like_secret(edit.get("content") or ""):
                violations.append("edit to %s appears to embed a secret" % rel)
                continue
            os.makedirs(os.path.dirname(full) or worktree, exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="") as fh:
                fh.write(edit.get("content") or "")
    return violations


def _snapshot(root, allowed_patterns):
    """Repository context for the worker: file list plus contents of files
    matching allowed paths (bounded)."""
    try:
        listing = gitops.run_git(["ls-files"], cwd=root).splitlines()[:500]
    except errors.AgenticError:
        listing = []
    files, budget_left = {}, MAX_SNAPSHOT_BYTES
    for rel in listing:
        if len(files) >= MAX_SNAPSHOT_FILES or budget_left <= 0:
            break
        if gitops.matches_any(rel, allowed_patterns):
            try:
                with open(os.path.join(root, rel), "r", encoding="utf-8",
                          errors="replace") as fh:
                    content = fh.read(budget_left)
            except OSError:
                continue
            files[rel] = content
            budget_left -= len(content)
    return {"file_list": listing, "files": files}


def _run_safe_commands(cfg, commands, cwd):
    """Model-requested commands: allowlist-verbatim only, never a shell."""
    allow = (cfg.get("execution", {}) or {}).get("safe_commands") or []
    timeout = int(cfg.get("execution", {}).get("command_timeout_seconds", 900))
    results = []
    for cmd in commands or []:
        run = execpolicy.run_allowlisted(cmd, allow, cwd, timeout)
        if run is None:
            results.append({"command": cmd, "skipped": "not on allowlist"})
            continue
        results.append({"command": cmd, "exit_code": run["exit_code"],
                        "output": (run["stdout"] + run["stderr"])[-1000:]})
    return results


def _disagreements(memory_dir):
    path = os.path.join(memory_dir, "disagreements.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), path
    return {}, path


def _bump_disagreement(memory_dir, skill, reset=False):
    data, path = _disagreements(memory_dir)
    data[skill] = 0 if reset else data.get(skill, 0) + 1
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return data[skill]


# -- the tick -----------------------------------------------------------------

def run_tick(cfg=None, dry_run=False, invoker=None, transport=None, env=None,
             run_id=None):
    cfg = cfg or load_config(env=env)
    p = _paths(cfg)
    run_id = run_id or new_run_id()
    run_dir = os.path.join(p["runs"], run_id)
    os.makedirs(run_dir, exist_ok=True)
    memory = p["memory"]
    budget = Budget(cfg, memory, run_id)
    trust_ledger = TrustLedger(cfg, memory)
    protected = gitops.load_protected_paths(cfg, p["agentic"])
    log = lambda event: logs.decision(memory, dict(event, run_id=run_id))

    def call(role, prompt, input_data, schema):
        # every prompt goes through the Context Broker; the raw input_data
        # is still handed to injected test invokers for observability
        from .context.broker import BrokerError
        from .context.compose import compose
        try:
            package = compose(cfg, role, prompt, input_data, schema,
                              memory_dir=memory)
        except BrokerError as exc:
            log({"event": "context_budget_stop", "role": role,
                 "detail": str(exc)[:300]})
            err = errors.PolicyError("context broker: %s" % exc)
            return {"ok": False, "provider": "?", "model": "?", "content": "",
                    "structured_output": {}, "usage": {"input_tokens": 0,
                                                       "output_tokens": 0,
                                                       "cached_tokens": 0},
                    "estimated_cost_usd": 0.0, "finish_reason": "error",
                    "refusal": False, "error": err.as_dict()}
        if invoker is not None:
            return invoker(role=role, prompt=package.rendered,
                           input_data=input_data, output_schema=schema,
                           budget=budget)
        return invoke_model(cfg, role, package.rendered, input_data=None,
                            output_schema=schema, budget=budget,
                            transport=transport, log=log, env=env)

    def finish(status, **extra):
        result = dict(extra, status=status, run_id=run_id)
        with open(os.path.join(run_dir, "result.json"), "w",
                  encoding="utf-8") as fh:
            fh.write(redact(json.dumps(result, indent=2, default=str)))
        logs.update_state(memory, run_id, ["status: %s" % status] +
                          ["%s: %s" % (k, str(v)[:200]) for k, v in extra.items()
                           if k in ("skill", "item", "reason", "branch")])
        for warning in budget.warnings:
            logs.alert(memory, "budget_warning", warning)
        return result

    # 0. budget gate before the run
    try:
        budget.check_before_run()
    except errors.BudgetExceededError as exc:
        logs.alert(memory, "budget", exc.detail)
        return finish("budget_stop", reason=exc.detail)

    # 1. context + standing goals
    context, goal_violations = gather_context(cfg, p)
    for violation in goal_violations:
        logs.alert(memory, "goal_violated",
                   "%s: %s" % (violation["id"], violation["detail"]))

    # 2. triage
    triage = call("triage", load_prompt("triage.md", shared=False), context,
                  _schema("triage.schema.json"))
    if not triage["ok"]:
        kind = (triage.get("error") or {}).get("kind", "?")
        if kind == "malformed_output":
            logs.alert(memory, "malformed_output",
                       "triage output unusable after repair and fallback")
        elif kind in errors.FALLBACK_KINDS or kind == "auth":
            logs.alert(memory, "providers_unavailable",
                       "triage failed: %s" % kind)
        return finish("triage_failed", reason=kind)
    findings = triage["structured_output"]
    log({"event": "triage", "status": findings["status"],
         "findings": len(findings["findings"])})
    actionable = [f for f in findings["findings"] if f["status"] == "actionable"]
    if findings["status"] == "quiet" or not actionable:
        return finish("quiet")

    # 3. conductor
    conductor_input = {
        "findings": findings["findings"],
        "trust_ledger": {s: r["current_tier"]
                         for s, r in trust_ledger.rows.items()},
        "budget": {"daily_spend_usd": round(budget.daily_spend(), 4),
                   "daily_limit_usd": budget.b.get("daily_limit_usd"),
                   "per_run_limit_usd": budget.b.get("per_run_limit_usd")},
        "limits": {"max_changed_lines":
                   cfg.get("execution", {}).get("max_changed_lines", 400)},
        "contract_must_queue": "see contract.md sections MUST QUEUE",
        "protected_path_patterns": protected[:50],
    }
    conducted = call("conductor", load_prompt("conductor.md", shared=False), conductor_input,
                     _schema("work-order.schema.json"))
    if not conducted["ok"]:
        return finish("conductor_failed",
                      reason=(conducted.get("error") or {}).get("kind"))
    order = conducted["structured_output"]
    with open(os.path.join(run_dir, "work-order.json"), "w",
              encoding="utf-8") as fh:
        json.dump(order, fh, indent=2)
    log({"event": "work_order", "action": order["action"],
         "skill": order["skill"], "risk": order["risk"]})

    # 4. code-enforced policy gate
    decision = apply_policy(cfg, order, trust_ledger, protected)
    if decision["action"] == "stop":
        return finish("stop", reason="; ".join(decision["reasons"]))
    if decision["action"] == "queue":
        qpath = queueing.enqueue(p["queue"], {
            "type": "work_order", "skill": order["skill"],
            "item": order["item"], "reason": "; ".join(decision["reasons"]),
            "work_order": order, "run_id": run_id})
        if any("protected" in r for r in decision["reasons"]):
            logs.alert(memory, "protected_path",
                       "work order queued: %s" % decision["reasons"])
        return finish("queued", skill=order["skill"],
                      reason="; ".join(decision["reasons"]), queue_file=qpath)

    if dry_run:
        return finish("dry_run", skill=order["skill"], item=order["item"],
                      work_order=order)

    # 5. isolated worktree
    worktree_enabled = bool(cfg.get("execution", {}).get("worktree_enabled", True))
    if worktree_enabled:
        try:
            worktree, branch = gitops.create_worktree(p["root"], p["agentic"],
                                                      run_id)
        except errors.AgenticError as exc:
            return finish("worktree_failed", reason=exc.detail)
    else:
        worktree, branch = p["root"], None

    # 6. baseline (recorded once, pre-edit)
    if gate.load_baseline(p["agentic"]) is None:
        pre = gate.run_checks(cfg, worktree,
                              os.path.join(run_dir, "baseline-checks"))
        gate.save_baseline(p["agentic"], pre["results"])
        log({"event": "baseline_recorded",
             "healthy": all(r["passed"] for r in pre["results"])})

    # 7. worker
    worker_input = {"work_order": order,
                    "repository": _snapshot(worktree, order["allowed_paths"])}
    worked = call("worker", load_prompt("implement.md", shared=False), worker_input,
                  _schema("worker.schema.json"))
    if not worked["ok"]:
        gitops.remove_worktree(p["root"], worktree, branch) if worktree_enabled else None
        return finish("worker_failed",
                      reason=(worked.get("error") or {}).get("kind"),
                      skill=order["skill"])
    wout = worked["structured_output"]
    if wout.get("blocked"):
        queueing.enqueue(p["queue"], {
            "type": "blocker", "skill": order["skill"], "item": order["item"],
            "reason": wout.get("blocker") or "worker blocked",
            "work_order": order, "run_id": run_id})
        if worktree_enabled:
            gitops.remove_worktree(p["root"], worktree, branch)
        return finish("blocked", skill=order["skill"],
                      reason=wout.get("blocker"))

    violations = apply_edits(worktree, wout.get("edits", []),
                             order["allowed_paths"],
                             order.get("forbidden_paths", []), protected)
    gitops.stage_all(worktree)
    lines = gitops.changed_lines(worktree)
    files = gitops.changed_files(worktree)
    violations += gitops.check_paths(files, order["allowed_paths"],
                                     order.get("forbidden_paths", []), protected)
    limit_lines = min(int(order["maximum_changed_lines"] or 0) or 10**9,
                      int(cfg.get("execution", {}).get("max_changed_lines", 400)))
    if lines > limit_lines:
        violations.append("changed lines %d exceed limit %d" % (lines, limit_lines))
    if len(files) > int(cfg.get("execution", {}).get("max_changed_files", 20)):
        violations.append("changed files %d exceed limit" % len(files))
    if violations:
        violations = sorted(set(violations))
        if any("protected" in v for v in violations):
            logs.alert(memory, "protected_path", "; ".join(violations[:3]))
        before, after = trust_ledger.record(order["skill"], False,
                                            worked["provider"], worked["model"])
        if before != after:
            logs.alert(memory, "trust_demoted",
                       "%s: %s -> %s" % (order["skill"], before, after))
        if worktree_enabled:
            gitops.remove_worktree(p["root"], worktree, branch)
        queueing.enqueue(p["queue"], {
            "type": "policy_violation", "skill": order["skill"],
            "item": order["item"], "reason": "; ".join(violations),
            "work_order": order, "run_id": run_id})
        return finish("policy_violation", skill=order["skill"],
                      reason="; ".join(violations))

    command_results = _run_safe_commands(cfg, wout.get("commands"), worktree)

    # 8. deterministic gate (final technical vote)
    gate_result = gate.run_checks(cfg, worktree, os.path.join(run_dir, "checks"))
    verdict_vs_baseline = gate.evaluate_against_baseline(p["agentic"], gate_result)
    log({"event": "deterministic_gate", "ok": verdict_vs_baseline["ok"],
         "regressions": verdict_vs_baseline["regressions"],
         "known_failing": verdict_vs_baseline["known_failing"]})

    # 9. independent verifier (fresh context: order + diff + check results only)
    verifier_input = {
        "work_order": order,
        "changed_files": files,
        "changed_lines": lines,
        "diff": redact(gitops.diff_text(worktree)),
        "deterministic_checks": {
            "ok": verdict_vs_baseline["ok"],
            "results": [{k: r[k] for k in ("name", "passed", "mandatory")}
                        for r in gate_result["results"]],
            "known_failing_baseline": verdict_vs_baseline["known_failing"]},
        "safe_command_results": command_results,
    }
    verified = call("verifier", load_prompt("verify.md", shared=False), verifier_input,
                    _schema("verification.schema.json"))
    verifier_out = verified["structured_output"] if verified["ok"] else None
    verdict = (verifier_out or {}).get("verdict", "uncertain")
    log({"event": "verifier", "verdict": verdict, "ok": verified["ok"]})

    gate_ok = verdict_vs_baseline["ok"]
    passed = gate_ok and verdict == "pass" and \
        bool((verifier_out or {}).get("test_integrity_preserved", False))

    # trust: deterministic gate failure or non-pass verdict is a failure
    before, after = trust_ledger.record(order["skill"], passed,
                                        worked["provider"], worked["model"])
    if before != after and not passed:
        logs.alert(memory, "trust_demoted",
                   "%s: %s -> %s" % (order["skill"], before, after))
    row = trust_ledger.rows[order["skill"]]
    if not passed and row["consecutive_failures"] >= 2:
        logs.alert(memory, "repeated_verification_failure",
                   "%s failed %d times in a row"
                   % (order["skill"], row["consecutive_failures"]))

    # maker/verifier disagreement: gate passed but verifier disagreed
    disagreement_forced_queue = False
    if gate_ok and verdict in ("fail", "uncertain") and not wout.get("blocked"):
        count = _bump_disagreement(memory, order["skill"])
        if count >= 2:
            disagreement_forced_queue = True
            logs.alert(memory, "maker_verifier_disagreement",
                       "%s: %d consecutive disagreements, queueing for human"
                       % (order["skill"], count))
    elif passed:
        _bump_disagreement(memory, order["skill"], reset=True)

    summary = {"skill": order["skill"], "item": order["item"],
               "branch": branch, "worktree": worktree,
               "gate_ok": gate_ok, "verifier_verdict": verdict,
               "verifier": verifier_out, "changed_lines": lines,
               "changed_files": files,
               "worker_summary": wout.get("summary", "")}

    if not passed or disagreement_forced_queue:
        # keep worktree for human inspection; queue it
        gitops.commit_all(worktree, "agentic draft (%s): %s [FAILED %s]"
                          % (order["skill"], order["item"][:60],
                             "gate" if not gate_ok else "verifier:" + verdict))
        queueing.enqueue(p["queue"], dict(
            summary, type="failed_or_disputed",
            reason=("deterministic gate failed: %s" % verdict_vs_baseline["regressions"]
                    if not gate_ok else "verifier verdict: %s" % verdict),
            run_id=run_id))
        return finish("failed" if not gate_ok else "needs_human",
                      **{k: summary[k] for k in ("skill", "item", "branch")},
                      reason="gate" if not gate_ok else "verdict:" + verdict)

    # 10. trust policy decides the destiny of verified work
    gitops.commit_all(worktree, "agentic (%s): %s"
                      % (order["skill"], order["item"][:60]))
    tier = trust_ledger.tier(order["skill"])
    mode = cfg.get("execution", {}).get("mode", "review")
    if mode == "auto" and tier == TIER_AUTO:
        # autonomous completion: branch stays committed locally. Push/merge is
        # contract-forbidden; a human (or explicitly-enabled integration) ships it.
        log({"event": "autonomous_complete", "skill": order["skill"],
             "branch": branch})
        return finish("autonomous_complete", **{k: summary[k] for k in
                                                ("skill", "item", "branch")})
    queueing.enqueue(p["queue"], dict(
        summary, type="verified_draft",
        reason="verified; awaiting approval (tier=%s, mode=%s)" % (tier, mode),
        run_id=run_id))
    return finish("draft_ready", **{k: summary[k] for k in
                                    ("skill", "item", "branch")},
                  tier=tier)
