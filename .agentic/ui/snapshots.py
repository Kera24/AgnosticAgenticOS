"""Read-only state builders for the dashboard API.

Every function reads the same YAML/JSON/TSV state the CLI commands use —
nothing is duplicated or re-derived in JavaScript. All text that may contain
model/CLI output is passed through the existing redaction before it can
reach the browser.
"""
import datetime as _dt
import json
import os

from core import config as config_mod
from core import gate, projstate
from core.breaker import BreakerBoard
from core.capacity import (CALL_COLUMNS, CapacityLedger, decide_start,
                           estimate_cycle_tokens)
from core.redact import redact
from core.scheduler import Scheduler

PROJECT_BRANCH = "agentic/project"

# Static registry of the specialised roles. Purpose/permissions are facts of
# the orchestration design (see core/project.py), not configuration.
ROLES = [
    {"id": "architect", "name": "Project Architect", "ai": True,
     "purpose": "Turns the application plan into architecture, milestones, "
                "a persistent backlog and acceptance criteria.",
     "can_edit": False, "permissions": "read-only", "conditional": False},
    {"id": "conductor", "name": "Conductor", "ai": True,
     "purpose": "Converts the next backlog task into a bounded work order "
                "with allowed paths and done-when criteria.",
     "can_edit": False, "permissions": "read-only", "conditional": False},
    {"id": "coder", "name": "Coder", "ai": True,
     "purpose": "Implements exactly one work order inside the isolated "
                "project worktree.",
     "can_edit": True, "permissions": "workspace-write", "conditional": False},
    {"id": "qa", "name": "QA Reviewer", "ai": True,
     "purpose": "Independent review with fresh context; verifies done-when "
                "criteria and test integrity. Never reviews its own edits.",
     "can_edit": False, "permissions": "read-only", "conditional": False},
    {"id": "security", "name": "Security Reviewer", "ai": True,
     "purpose": "Conditional review, triggered by security-relevant paths, "
                "diff patterns or task flags.",
     "can_edit": False, "permissions": "read-only", "conditional": True},
    {"id": "gate", "name": "Deterministic Gate", "ai": False,
     "purpose": "Runs the repository's real checks (tests/build/lint). "
                "Not an AI agent — no model can override its verdict.",
     "can_edit": False, "permissions": "executes configured checks",
     "conditional": False},
]

AI_ROLE_IDS = [r["id"] for r in ROLES if r["ai"]]


def _agentic():
    return str(config_mod.AGENTIC_DIR)


def _memory():
    return os.path.join(_agentic(), "memory")


def _runs_dir():
    return os.path.join(_agentic(), "runs")


def _worktree_path():
    return os.path.join(_agentic(), "worktrees", "project")


# -- project ------------------------------------------------------------------

def project_snapshot(cfg):
    a = _agentic()
    exists = projstate.exists(a)
    scheduler = Scheduler(cfg, _memory())
    snap = {
        "exists": exists,
        "name": (cfg.get("project") or {}).get("name"),
        "scheduler": scheduler.state,
        "eligible": None, "eligible_reason": None,
        "progress": {}, "blockers": [], "human_blockers": [],
        "milestones": [], "backlog_summary": {}, "next_task": None,
        "branch": PROJECT_BRANCH,
        "worktree": _worktree_path(),
        "worktree_exists": os.path.exists(
            os.path.join(_worktree_path(), ".git")),
        "repository_root": str(config_mod.repo_root(cfg)),
        "final_audit": None,
        "human_decisions": [],
    }
    ok, reason = scheduler.eligible()
    snap["eligible"], snap["eligible_reason"] = ok, reason
    if not exists:
        return snap
    snap["progress"] = projstate.read_yaml(a, "progress.yaml", {}) or {}
    snap["blockers"] = projstate.open_blockers(a)
    snap["human_blockers"] = projstate.open_blockers(a, human_only=True)
    snap["milestones"] = (projstate.read_yaml(a, "milestones.yaml",
                                              {"milestones": []})
                          or {}).get("milestones", [])
    tasks = projstate.load_backlog(a)
    by_status = {}
    for t in tasks:
        by_status.setdefault(t["status"], []).append(t["id"])
    snap["backlog_summary"] = {k: len(v) for k, v in by_status.items()}
    nxt = projstate.next_task(a)
    snap["next_task"] = _public_task(nxt) if nxt else None
    snap["final_audit"] = projstate.read_yaml(a, "final-audit.yaml", None)
    decisions = projstate.read_yaml(a, "decisions.yaml", {}) or {}
    snap["human_decisions"] = decisions.get("human_decisions_needed", [])
    return snap


def _public_task(task):
    keys = ("id", "milestone", "description", "dependencies", "risk",
            "security_relevant", "expected_size", "status", "attempts",
            "last_result", "blocking_reason", "acceptance_criteria")
    return {k: task.get(k) for k in keys}


def backlog(cfg):
    a = _agentic()
    if not projstate.exists(a):
        return []
    return [_public_task(t) for t in projstate.load_backlog(a)]


def plan_documents(cfg):
    a = _agentic()
    out = {}
    for key, name in (("plan", "PROJECT.md"),
                      ("architecture", "architecture.md")):
        path = os.path.join(projstate.project_dir(a), name)
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                out[key] = redact(fh.read(200_000))
        else:
            out[key] = None
    criteria = projstate.read_yaml(a, "acceptance-criteria.yaml", {}) or {}
    out["acceptance_criteria"] = criteria
    return out


# -- agents ---------------------------------------------------------------------

def _routing_chain_safe(cfg, role):
    from core.backends import routing_chain
    try:
        return routing_chain(cfg, role)
    except Exception:
        return []


def agents_snapshot(cfg):
    ledger = CapacityLedger(cfg, _memory())
    rows = ledger._rows(ledger.calls_path, CALL_COLUMNS)
    by_role = {}
    for row in rows:
        by_role.setdefault(row["role"], []).append(row)
    out = []
    for role in ROLES:
        entry = dict(role)
        history = by_role.get(role["id"], [])
        recent = history[-20:]
        entry["chain"] = (_routing_chain_safe(cfg, role["id"])
                          if role["ai"] else [])
        entry["last_invocation"] = recent[-1]["timestamp"] if recent else None
        entry["last_backend"] = recent[-1]["backend"] if recent else None
        entry["last_ok"] = (recent[-1]["ok"] == "1") if recent else None
        entry["recent_calls"] = len(recent)
        entry["recent_failures"] = sum(1 for r in recent if r["ok"] != "1")
        durations = [float(r["duration_seconds"] or 0) for r in recent
                     if r.get("duration_seconds")]
        entry["recent_avg_duration_seconds"] = (
            round(sum(durations) / len(durations), 1) if durations else None)
        tokens = 0
        estimated = False
        for r in recent:
            for col in ("input_tokens", "output_tokens", "reasoning_tokens"):
                if r.get(col):
                    try:
                        tokens += int(float(r[col]))
                    except ValueError:
                        pass
            if r.get("estimated") == "1":
                estimated = True
        entry["recent_tokens"] = tokens
        entry["tokens_estimated"] = estimated
        out.append(entry)
    return out


# -- backends --------------------------------------------------------------------

def backends_snapshot(cfg, detected, apis):
    """Combine configured backends, detection results (may be cached), the
    circuit-breaker board and routing assignments. Auth status is whatever
    the CLI itself reports — `unknown` is NEVER treated as authenticated."""
    board = BreakerBoard(_memory())
    routing = cfg.get("routing") or {}
    assigned = {}
    for role_id in AI_ROLE_IDS:
        for backend in _routing_chain_safe(cfg, role_id):
            assigned.setdefault(backend, []).append(role_id)
    out = []
    for name, bcfg in (cfg.get("backends") or {}).items():
        btype = (bcfg or {}).get("type", "api")
        info = detected.get(name) or {}
        breaker = board.entry(name) if name in board.data else None
        item = {
            "name": name, "type": btype,
            "classification": {"cli": "cli", "local": "local"}.get(
                btype, "api"),
            "detected": bool(info.get("installed")),
            "version": info.get("version"),
            "auth": info.get("auth", "unknown" if btype != "local"
                             else "not-required"),
            "models": info.get("models", []),
            "model": (bcfg or {}).get("model"),
            "smoke_test_passed": (bcfg or {}).get("smoke_test_passed"),
            "breaker": breaker,
            "breaker_state": board.state(name) if breaker else "available",
            "unavailable_until": (breaker or {}).get("unavailable_until"),
            "last_ok": (breaker or {}).get("last_ok"),
            "last_failure_kind": (breaker or {}).get("last_failure_kind"),
            "consecutive_failures": (breaker or {}).get(
                "consecutive_failures", 0),
            "roles": sorted(set(assigned.get(name, []))),
            "is_primary": routing.get("primary") == name,
            "in_fallbacks": name in (routing.get("fallbacks") or []),
            "usable": None,
        }
        if btype == "cli":
            item["usable"] = bool(info.get("installed")) and \
                info.get("auth") == "ok"
        elif btype == "local":
            item["usable"] = bool(info.get("installed"))
        out.append(item)
    for pname, info in (apis or {}).items():
        out.append({
            "name": pname, "type": "api", "classification": "api",
            "detected": True, "version": None,
            "auth": "ok" if info.get("configured") else "key-not-set",
            "models": [], "model": None, "smoke_test_passed": None,
            "breaker": None, "breaker_state": "available",
            "unavailable_until": None, "last_ok": None,
            "last_failure_kind": None, "consecutive_failures": 0,
            "roles": sorted(set(assigned.get(pname, []))),
            "is_primary": routing.get("primary") == pname,
            "in_fallbacks": pname in (routing.get("fallbacks") or []),
            "usable": bool(info.get("configured")),
            "api_key_env": info.get("api_key_env"),
        })
    return out


# -- capacity ----------------------------------------------------------------------

def capacity_snapshot(cfg):
    a = _agentic()
    ledger = CapacityLedger(cfg, _memory())
    board = BreakerBoard(_memory())
    task = projstate.next_task(a) if projstate.exists(a) else None
    routing = cfg.get("routing") or {}
    chain = ([routing["primary"]] + list(routing.get("fallbacks") or [])
             if routing.get("primary") else [])
    per_backend = []
    for name in (cfg.get("backends") or {}):
        calls_hour = ledger.calls_in_window(name, 1)
        calls_day = ledger.calls_in_window(name, 24)
        tokens = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
        any_estimated = False
        for row in calls_day:
            for key, col in (("input", "input_tokens"),
                             ("cached", "cached_input_tokens"),
                             ("output", "output_tokens"),
                             ("reasoning", "reasoning_tokens")):
                if row.get(col):
                    try:
                        tokens[key] += int(float(row[col]))
                    except ValueError:
                        pass
            if row.get("estimated") == "1":
                any_estimated = True
        per_backend.append({
            "name": name,
            "calls_last_hour": len(calls_hour),
            "calls_last_day": len(calls_day),
            "tokens_last_day": tokens,
            "tokens_estimated": any_estimated,
            "limit_reasons": ledger.limit_status(name),
            "remaining_under_limits": ledger.remaining_by_limits(name),
            "limits": (cfg.get("limits") or {}).get(name) or {},
            "breaker_state": board.state(name) if name in board.data
            else "available",
            "unavailable_until": board.unavailable_until(name),
        })
    estimate = (estimate_cycle_tokens(cfg, task or {}, ledger, chain[0])
                if chain else None)
    decision = (decide_start(cfg, task or {}, ledger, board, chain)
                if chain else None)
    cycles = ledger.recent_cycles(limit=20)
    events = _capacity_events(limit=25)
    return {
        "note": "Subscription CLI capacity is estimated from local history "
                "unless the backend reports exact usage or reset "
                "information.",
        "next_task": (task or {}).get("id"),
        "chain": chain,
        "estimate": estimate,
        "decision": decision,
        "safety_multiplier": (cfg.get("capacity")
                              or {}).get("safety_multiplier", 1.35),
        "per_backend": per_backend,
        "recent_cycles": cycles,
        "moving_average_cycle_tokens": _avg_tokens(cycles),
        "limit_events": events,
    }


def _avg_tokens(cycles):
    vals = []
    for row in cycles:
        try:
            vals.append(int(float(row.get("total_tokens") or 0)))
        except ValueError:
            pass
    return int(sum(vals) / len(vals)) if vals else None


def _capacity_events(limit=25):
    out = []
    for entry in activity_entries(limit=1000):
        kind = entry.get("kind") or entry.get("event")
        if entry.get("event") == "backend_error" and \
                entry.get("kind") in ("rate_limit", "usage_limit"):
            out.append(entry)
        elif entry.get("event") in ("capacity_decision",) and \
                entry.get("decision") in ("wait", "human_required"):
            out.append(entry)
        _ = kind
    return out[-limit:]


# -- verification -----------------------------------------------------------------

def verification_snapshot(cfg):
    a = _agentic()
    workdir = _worktree_path() if os.path.exists(_worktree_path()) \
        else str(config_mod.repo_root(cfg))
    commands, auto = gate.resolve_commands(cfg, workdir)
    baseline = gate.load_baseline(a)
    latest = _latest_check_results()
    verdicts = _recent_verdicts()
    known_failing = {name for name, passed in
                     ((baseline or {}).get("checks") or {}).items()
                     if passed is False}
    for check in latest.get("results", []):
        check["known_baseline_failure"] = check["name"] in known_failing
        check["new_regression"] = (not check["passed"]
                                   and check["name"] not in known_failing)
    return {
        "configured": bool(commands),
        "auto_detected": auto,
        "commands": [{"name": c["name"], "command": c["command"],
                      "mandatory": bool(c.get("mandatory", True))}
                     for c in commands],
        "no_checks_is_blocking": True,
        "baseline": baseline,
        "latest": latest,
        "qa": verdicts.get("qa_review"),
        "security": verdicts.get("security_review"),
        "final_audit": projstate.read_yaml(a, "final-audit.yaml", None),
    }


def _latest_check_results():
    runs = _runs_dir()
    if not os.path.isdir(runs):
        return {"run": None, "results": []}
    cycles = sorted((d for d in os.listdir(runs)
                     if d.startswith(("cycle-", "final-audit"))),
                    reverse=True)
    for cycle in cycles:
        cycle_dir = os.path.join(runs, cycle)
        check_dirs = sorted((d for d in os.listdir(cycle_dir)
                             if d.startswith("checks-")), reverse=True) \
            if os.path.isdir(cycle_dir) else []
        candidates = check_dirs or ([""] if any(
            f.endswith(".log") for f in os.listdir(cycle_dir)) else [])
        for sub in candidates:
            log_dir = os.path.join(cycle_dir, sub) if sub else cycle_dir
            results = _parse_check_logs(log_dir, cycle, sub)
            if results:
                return {"run": cycle, "attempt_dir": sub or None,
                        "results": results}
    return {"run": None, "results": []}


def _parse_check_logs(log_dir, run, sub):
    results = []
    try:
        names = sorted(os.listdir(log_dir))
    except OSError:
        return []
    for fname in names:
        if not fname.endswith(".log"):
            continue
        path = os.path.join(log_dir, fname)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                content = fh.read(200_000)
        except OSError:
            continue
        exit_code = None
        for line in content.splitlines()[:4]:
            if line.startswith("exit:"):
                raw = line.split(":", 1)[1].strip()
                try:
                    exit_code = int(raw)
                except ValueError:
                    exit_code = None
        results.append({
            "name": fname[:-4], "run": run,
            "log_file": fname,
            "exit_code": exit_code,
            "passed": exit_code == 0,
            "excerpt": redact(content[-1500:]),
        })
    return results


def _recent_verdicts():
    out = {}
    for entry in activity_entries(limit=800):
        if entry.get("event") in ("qa_review", "security_review"):
            out[entry["event"]] = entry
    return out


# -- activity -----------------------------------------------------------------------

def activity_entries(limit=300):
    """Tail of decisions.jsonl (already redacted at write time; redacted
    again on malformed lines as defence in depth)."""
    path = os.path.join(_memory(), "decisions.jsonl")
    if not os.path.exists(path):
        return []
    entries = []
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            if size > 2_000_000:
                fh.seek(size - 2_000_000)
                fh.readline()   # skip partial line
            lines = fh.read().splitlines()
    except OSError:
        return []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            entries.append({"event": "malformed",
                            "raw": redact(line)[:2000]})
    return entries


# -- log file access (validated) --------------------------------------------------

class LogAccessError(Exception):
    pass


def read_run_log(run, name):
    """Read one check log from .agentic/runs/<run>/(checks-*/)?<name>.log.
    Both parts are validated against a strict charset and the resolved path
    must stay inside the runs directory — no arbitrary filesystem access."""
    import re
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", run or "") or \
            not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", name or ""):
        raise LogAccessError("invalid run or log name")
    if ".." in run or ".." in name:
        raise LogAccessError("invalid run or log name")
    runs = os.path.realpath(_runs_dir())
    base = os.path.realpath(os.path.join(runs, run))
    if not (base == runs or base.startswith(runs + os.sep)) or \
            not os.path.isdir(base):
        raise LogAccessError("unknown run")
    candidates = [os.path.join(base, name + ".log")]
    for sub in sorted(os.listdir(base), reverse=True):
        if sub.startswith("checks-"):
            candidates.append(os.path.join(base, sub, name + ".log"))
    for path in candidates:
        real = os.path.realpath(path)
        if not real.startswith(runs + os.sep):
            continue
        if os.path.isfile(real):
            with open(real, encoding="utf-8", errors="replace") as fh:
                if os.path.getsize(real) > 1_000_000:
                    fh.seek(os.path.getsize(real) - 1_000_000)
                return {"run": run, "name": name,
                        "content": redact(fh.read())}
    raise LogAccessError("log not found")


# -- misc --------------------------------------------------------------------------

def now_iso():
    return _dt.datetime.now().isoformat(timespec="seconds")
