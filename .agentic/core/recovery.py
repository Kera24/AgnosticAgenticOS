"""Persisted-state recovery pipeline (item 4 of the aggregate
parallel-candidate blocker fix).

`project recover <project-id>` used to run exactly one stage (stale
owned-process reconciliation). A project stuck on an aggregate Phase-5
blocker had no path back to health short of hand-editing state files.
`run_recovery` is the single, ordered pipeline every `recover` invocation
now runs; every stage returns a structured result, even when it finds
nothing to do, so a caller can always see exactly what was (or wasn't)
recovered."""
import glob
import json
import os
import re
import shutil

from . import bootstrap_gate, contract_recovery, parallel_recovery
from . import logs, projstate, taskspace


def _stage(name, events, extra=None):
    result = {"stage": name, "events": events, "count": len(events)}
    if extra:
        result.update(extra)
    return result


# -- 2. stale lease recovery ----------------------------------------------------

def _stale_lease_recovery(agentic_dir, cfg, clock):
    """An expired lease is always safe to mark released -- its own TTL
    already guarantees no other holder could be relying on it being
    active. Distinct from (and complementary to) the PID-verified
    stale-owned-process reconciliation, which handles a lease/lock whose
    OWNING PROCESS died before it could release cleanly."""
    lease = taskspace.ProjectLease(
        agentic_dir, cfg.get("project", {}).get("name"), clock=clock)
    raw = lease._read()   # noqa: SLF001 -- read-only status inspection
    if not raw or raw.get("status") != "active":
        return _stage("stale_lease_recovery", [],
                      extra={"detail": "no active lease on record"})
    if lease.holder() is not None:
        return _stage("stale_lease_recovery", [],
                      extra={"detail": "lease still valid, not stale"})
    raw["status"] = "released"
    lease._write(raw)   # noqa: SLF001 -- same file this class owns
    return _stage("stale_lease_recovery",
                  [{"action": "released_expired_lease"}])


# -- fixed native-Windows Codex sandbox blocker -------------------------------

_WINDOWS_CODEX_READ_ONLY_RE = re.compile(
    r"^(?:Workspace is read-only, so the required scaffold files and "
    r"directories cannot be created|Workspace filesystem is read-only, "
    r"so required edits(?:\s+.*?)?\s+cannot be made)\.?$", re.I)


def _is_native_windows():
    return os.name == "nt"


def _is_windows_codex_readonly_detail(detail):
    """Match only the exact persisted live failure produced by the broken
    native-Windows Codex sandbox. This is deliberately narrower than a bare
    read-only substring so a genuine repository policy denial is never
    silently cleared."""
    return bool(_WINDOWS_CODEX_READ_ONLY_RE.match((detail or "").strip()))


def recover_windows_codex_readonly_blocker(agentic_dir, cfg):
    """Clear the stale blocker once the Windows Codex sandbox fix is active.

    Recovery is permitted only on native Windows and only when the machine
    explicitly loads Codex user config (ignore_user_config: false), which is
    where the elevated Windows sandbox selection lives. It never marks work
    done; it resets the affected task to pending and records the old blocker
    as a resolved platform-owned workspace-policy failure.
    """
    codex = ((cfg or {}).get("backends") or {}).get("codex") or {}
    if not _is_native_windows() or \
            codex.get("ignore_user_config") is not False:
        return []
    if not projstate.exists(agentic_dir):
        return []

    blockers_doc = projstate.read_yaml(
        agentic_dir, "blockers.yaml", {"blockers": []})
    blockers = blockers_doc.get("blockers", [])
    events = []
    for task in projstate.load_backlog(agentic_dir):
        if task.get("status") != "blocked" or not \
                _is_windows_codex_readonly_detail(
                    task.get("blocking_reason")):
            continue
        resolved = 0
        for blocker in blockers:
            if blocker.get("resolved") or blocker.get("task") != task["id"]:
                continue
            if not _is_windows_codex_readonly_detail(blocker.get("reason")):
                continue
            blocker.update({
                "resolved": True,
                "code": projstate.BLOCKER_CODE_POLICY_DENIED,
                "failure_class": "workspace_policy_denied",
                "platform_owned": True,
                "retryable": True,
            })
            resolved += 1
        projstate.update_task(
            agentic_dir, task["id"], status="pending",
            blocking_reason=None, last_result=None)
        events.append({
            "task_id": task["id"],
            "action": "reset_windows_codex_readonly_blocker",
            "resolved_blockers": resolved,
        })
    if events:
        projstate.write_yaml(agentic_dir, "blockers.yaml", blockers_doc)
    return events


# -- fixed Windows command-shim resolution blocker ---------------------------

_DETERMINISTIC_REPAIR_EXHAUSTED_REASONS = {
    "deterministic checks failing after 3 attempts",
    "repair attempts exhausted",
}


def _windows_command_available(command):
    return bool(shutil.which(command))


def _command_resolution_evidence(agentic_dir, task_id):
    """Find the newest persisted gate result proving a bare npm lookup failed.

    The generic task blocker does not retain individual gate details, so
    recovery consults immutable cycle artifacts. It never relies on model
    prose and never clears a normal failing-test result.
    """
    runs_root = os.path.join(str(agentic_dir), "runs")
    cycle_dirs = sorted(
        glob.glob(os.path.join(runs_root, "cycle-*")),
        key=lambda p: os.path.getmtime(p), reverse=True)
    for cycle_dir in cycle_dirs:
        order_path = os.path.join(cycle_dir, "work-order.json")
        try:
            with open(order_path, encoding="utf-8") as fh:
                order = json.load(fh)
        except (OSError, ValueError):
            continue
        if order.get("item") != task_id:
            continue
        for path in sorted(glob.glob(
                os.path.join(cycle_dir, "validation-result-*.json")),
                reverse=True):
            try:
                with open(path, encoding="utf-8") as fh:
                    result = json.load(fh)
            except (OSError, ValueError):
                continue
            for check in result.get("results") or []:
                command = str(check.get("command") or "")
                detail = str(check.get("detail") or "")
                if check.get("exit_code") == 127 and \
                        command.lower().startswith("npm ") and \
                        "command not found: npm" in detail.lower():
                    return {
                        "run_id": os.path.basename(cycle_dir).replace(
                            "cycle-", "", 1),
                        "evidence_ref": path,
                        "command": command,
                    }
        return None
    return None


def recover_windows_command_resolution_blocker(agentic_dir, cfg):
    """Retry a generic gate-exhaustion blocker only when persisted evidence
    proves the now-fixed Windows npm/PATHEXT resolution defect."""
    if not _is_native_windows() or not _windows_command_available("npm"):
        return []
    if not projstate.exists(agentic_dir):
        return []

    blockers_doc = projstate.read_yaml(
        agentic_dir, "blockers.yaml", {"blockers": []})
    blockers = blockers_doc.get("blockers", [])
    events = []
    for task in projstate.load_backlog(agentic_dir):
        if task.get("status") != "blocked" or \
                task.get("blocking_reason") not in _DETERMINISTIC_REPAIR_EXHAUSTED_REASONS:
            continue
        evidence = _command_resolution_evidence(agentic_dir, task["id"])
        if not evidence:
            continue
        resolved = 0
        for blocker in blockers:
            if blocker.get("resolved") or blocker.get("task") != task["id"]:
                continue
            if blocker.get("reason") not in _DETERMINISTIC_REPAIR_EXHAUSTED_REASONS:
                continue
            blocker.update({
                "resolved": True,
                "code": projstate.BLOCKER_CODE_POLICY_DENIED,
                "failure_class": "platform_capability_missing",
                "platform_owned": True,
                "retryable": True,
                "evidence_ref": evidence["evidence_ref"],
            })
            resolved += 1
        projstate.update_task(
            agentic_dir, task["id"], status="pending",
            blocking_reason=None, last_result=None)
        events.append({
            "task_id": task["id"],
            "action": "reset_windows_command_resolution_blocker",
            "resolved_blockers": resolved,
            **evidence,
        })
    if events:
        projstate.write_yaml(agentic_dir, "blockers.yaml", blockers_doc)
    return events



# -- fixed omission of canonical task-specific deterministic checks ------------

_QA_MISSING_TASK_GATE_RE = re.compile(
    r"^QA: .*required deterministic .*gate is not evidenced", re.I)


def _missing_task_gate_evidence(agentic_dir, task_id):
    runs_root = os.path.join(str(agentic_dir), "runs")
    cycle_dirs = sorted(
        glob.glob(os.path.join(runs_root, "cycle-*")),
        key=lambda p: os.path.getmtime(p), reverse=True)
    for cycle_dir in cycle_dirs:
        contract_path = os.path.join(cycle_dir, "task-contract.json")
        try:
            with open(contract_path, encoding="utf-8") as fh:
                contract = json.load(fh)
        except (OSError, ValueError):
            continue
        if contract.get("task_id") != task_id:
            continue
        required = [str(command) for command in
                    contract.get("deterministic_checks") or []]
        observed = set()
        validation_paths = sorted(glob.glob(
            os.path.join(cycle_dir, "validation-result-*.json")))
        for validation_path in validation_paths:
            try:
                with open(validation_path, encoding="utf-8") as fh:
                    validation = json.load(fh)
            except (OSError, ValueError):
                continue
            observed.update(str(result.get("command") or "")
                            for result in validation.get("results") or [])
        missing = [command for command in required if command not in observed]
        if missing:
            return {
                "run_id": contract.get("run_id") or
                          os.path.basename(cycle_dir).replace("cycle-", "", 1),
                "evidence_ref": contract_path,
                "missing_commands": missing,
                "observed_commands": sorted(observed),
            }
        return None
    return None


def recover_missing_task_gate_blocker(agentic_dir, cfg, memory_dir):
    """Retry only when artifacts prove the canonical task check was omitted."""
    if not projstate.exists(agentic_dir):
        return []
    blockers_doc = projstate.read_yaml(
        agentic_dir, "blockers.yaml", {"blockers": []})
    blockers = blockers_doc.get("blockers", [])
    backlog = {task["id"]: task for task in projstate.load_backlog(agentic_dir)}
    events = []
    for task_id, task in backlog.items():
        reason = task.get("blocking_reason") or ""
        if task.get("status") != "blocked" or not \
                _QA_MISSING_TASK_GATE_RE.search(reason):
            continue
        evidence = _missing_task_gate_evidence(agentic_dir, task_id)
        if not evidence:
            continue
        resolved = 0
        for blocker in blockers:
            if blocker.get("resolved") or blocker.get("task") != task_id:
                continue
            if not _QA_MISSING_TASK_GATE_RE.search(
                    blocker.get("reason") or ""):
                continue
            blocker.update({
                "resolved": True,
                "code": projstate.BLOCKER_CODE_STRUCTURAL_CONTRACT_MISMATCH,
                "failure_class": "task_contract_invalid",
                "platform_owned": True,
                "retryable": True,
                "evidence_ref": evidence["evidence_ref"],
            })
            resolved += 1
        projstate.update_task(agentic_dir, task_id, status="pending",
                              blocking_reason=None, last_result=None,
                              attempts=0)
        events.append({
            "task_id": task_id,
            "action": "reset_missing_task_deterministic_gate",
            "resolved_blockers": resolved,
            **evidence,
        })
    if events:
        projstate.write_yaml(agentic_dir, "blockers.yaml", blockers_doc)
    return events


# -- 6. task-state reconciliation -----------------------------------------------

def _task_state_reconciliation(agentic_dir):
    """A task left `in_progress` with no active file-ownership claim is
    unambiguously stuck (every normal exit path -- success, `fail()`,
    stale-owned-process recovery -- releases the claim before the run
    ends): reset it to pending so the next cycle can pick it up again."""
    backlog = projstate.load_backlog(agentic_dir)
    claims = taskspace.active_claims(agentic_dir)
    reconciled = []
    for task in backlog:
        if task["status"] == "in_progress" and task["id"] not in claims:
            projstate.update_task(agentic_dir, task["id"], status="pending")
            reconciled.append({"task_id": task["id"],
                               "action": "reset_in_progress_to_pending"})
    return reconciled


# -- 7. dependency reconciliation ------------------------------------------------

def _dependency_reconciliation(agentic_dir):
    """Pending tasks whose dependencies are now all done -- purely a
    report (item 5's "recalculate dependent task eligibility"):
    `projstate.next_task` already re-evaluates this on every cycle, so no
    task ever needs to be MUTATED here, only surfaced."""
    backlog = projstate.load_backlog(agentic_dir)
    done = {t["id"] for t in backlog if t["status"] == "done"}
    eligible = [t["id"] for t in backlog
               if t["status"] == "pending" and t.get("dependencies") and
               all(dep in done for dep in t["dependencies"])]
    return [{"task_id": tid, "action": "dependencies_satisfied"}
           for tid in eligible]


# -- 8. failure-streak reconciliation --------------------------------------------

def _is_legacy_platform_cycle_detail(detail_text):
    """Retroactive platform-cause recognition for a HISTORICAL
    `cycle_finished` log entry that predates `failure_classified`
    instrumentation (item 6) -- the exact same known-signature evidence
    `parallel_recovery`'s blocker migration uses, plus the two
    longer-standing bootstrap_gate legacy signatures. Never a bare
    "parallel" substring match."""
    text = detail_text or ""
    if parallel_recovery.legacy_platform_signature(text):
        return True
    if bootstrap_gate._is_legacy_deterministic_checks_block(   # noqa: SLF001
            {"blocking_reason": text}):
        return True
    if bootstrap_gate._is_legacy_expected_paths_contract_block(   # noqa: SLF001
            {"blocking_reason": text}):
        return True
    if _is_windows_codex_readonly_detail(text):
        return True
    if _QA_MISSING_TASK_GATE_RE.search(text):
        return True
    return False


def reconstruct_failure_streak(agentic_dir, scheduler):
    """Recompute `scheduler.state["failure_streak"]` from
    `decisions.jsonl`'s structured history (item 6): walk backward from
    the most recent cycle to the last success, and for every failing
    cycle in between, determine platform-ness from its own
    `failure_classified` event when one was logged, or (for cycles that
    predate that instrumentation) the same legacy signature evidence used
    to migrate blockers. A platform-caused failure never counts toward
    the streak; every genuine model/provider failure in the window is
    always retained -- this never blindly zeroes the streak."""
    memory_dir = os.path.join(str(agentic_dir), "memory")
    path = os.path.join(memory_dir, "decisions.jsonl")
    events = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    classified_by_run = {e["run_id"]: e for e in events
                        if e.get("event") == "failure_classified" and
                        e.get("run_id")}
    genuine, platform_removed = [], []
    for event in reversed(events):
        if event.get("event") != "cycle_finished":
            continue
        outcome = event.get("outcome")
        if outcome == "success":
            break   # the streak resets here -- nothing further back counts
        if outcome in ("rate_limit", "usage_limit"):
            continue   # never affected the streak in the first place
        run_id = event.get("run_id")
        classified = classified_by_run.get(run_id)
        # Known persisted platform signatures remain authoritative even when
        # an older classifier incorrectly recorded platform_class=false.
        is_platform = _is_legacy_platform_cycle_detail(event.get("detail")) or \
            bool(classified and classified.get("platform_class"))
        record = {"run_id": run_id, "task": event.get("task_id"),
                 "outcome": outcome, "detail": event.get("detail")}
        (platform_removed if is_platform else genuine).append(record)
    old_streak = int(scheduler.state.get("failure_streak") or 0)
    new_streak = len(genuine)
    scheduler.state["failure_streak"] = new_streak
    scheduler.save()
    cooling = scheduler.cooldown_breakdown("failure",
                                           failure_streak=new_streak)
    return {"previous_streak": old_streak, "resulting_streak": new_streak,
           "retained_genuine_failures": genuine,
           "removed_platform_failures": platform_removed,
           "resulting_cooling": cooling}


# -- 9. worktree compatibility validation ----------------------------------------

def _worktree_compatibility_validation(agentic_dir):
    """Reports every preserved (uncommitted-changes) task/candidate
    worktree belonging to a task that is now pending/blocked -- the real
    scope-compatibility check needs the CURRENT cycle's work order (only
    available at dispatch time; see project.py), so this stage surfaces
    presence/evidence for review rather than re-guessing compatibility
    outside of a cycle."""
    from . import gitops
    backlog = projstate.load_backlog(agentic_dir)
    preserved = []
    for task in backlog:
        if task["status"] not in ("pending", "blocked"):
            continue
        for candidate_id in (task["id"], "%s--c2" % task["id"]):
            wt_path = taskspace.task_worktree_path(agentic_dir, candidate_id)
            if not os.path.exists(os.path.join(wt_path, ".git")):
                continue
            changed = gitops.filter_tool_artifacts(
                gitops.changed_files(wt_path))
            if changed:
                preserved.append({"task_id": task["id"],
                                  "candidate_id": candidate_id,
                                  "changed_files": changed})
    return preserved


# -- pipeline --------------------------------------------------------------------

def run_recovery(cfg, agentic_dir, root, scheduler, clock, log):
    """Runs every recovery stage in order, returns a dict keyed by stage
    name -- always present, even when a stage found nothing to recover."""
    from .project import _recover_stale_owned_processes

    stages = {}
    owned = _recover_stale_owned_processes(cfg, agentic_dir, root, scheduler,
                                           clock, log) or []
    stages["stale_owned_process_recovery"] = _stage(
        "stale_owned_process_recovery", owned)
    stages["stale_lease_recovery"] = _stale_lease_recovery(
        agentic_dir, cfg, clock)

    windows_codex_events = recover_windows_codex_readonly_blocker(
        agentic_dir, cfg)
    windows_command_events = recover_windows_command_resolution_blocker(
        agentic_dir, cfg)
    memory_dir = os.path.join(str(agentic_dir), "memory")
    task_gate_events = recover_missing_task_gate_blocker(
        agentic_dir, cfg, memory_dir)
    migrated = list(bootstrap_gate.recover_bootstrap_deadlock(agentic_dir))
    migrated += list(bootstrap_gate.recover_expected_paths_contract_bug(
        agentic_dir))
    migrated += windows_codex_events
    migrated += windows_command_events
    migrated += task_gate_events
    stages["blocker_code_migration"] = _stage("blocker_code_migration",
                                              migrated)

    aggregate_events = parallel_recovery.recover_parallel_candidate_aggregate(
        agentic_dir)
    for event in windows_command_events + task_gate_events:
        logs.decision(memory_dir, {
            "event": "failure_classified",
            "run_id": event["run_id"],
            "task_id": event["task_id"],
            "failure_class": (
                "platform_capability_missing"
                if event.get("action") ==
                "reset_windows_command_resolution_blocker"
                else "task_contract_invalid"),
            "platform_class": True,
            "corrected_by": event.get("action"),
            "evidence_ref": event["evidence_ref"],
        })
    contract_events = contract_recovery.recover_contract_divergence_blockers(
        agentic_dir, memory_dir, cfg)
    contract_events += \
        contract_recovery.recover_fixed_contract_comparison_blockers(
            agentic_dir, memory_dir, cfg)
    stages["aggregate_candidate_cause_reconstruction"] = _stage(
        "aggregate_candidate_cause_reconstruction", aggregate_events)
    stages["fixed_platform_defect_recovery"] = _stage(
        "fixed_platform_defect_recovery",
        [e for e in aggregate_events if e.get("recovered")] +
        contract_events + windows_codex_events + windows_command_events +
        task_gate_events)

    stages["task_state_reconciliation"] = _stage(
        "task_state_reconciliation",
        _task_state_reconciliation(agentic_dir))
    stages["dependency_reconciliation"] = _stage(
        "dependency_reconciliation", _dependency_reconciliation(agentic_dir))
    stages["failure_streak_reconciliation"] = _stage(
        "failure_streak_reconciliation", [],
        extra=reconstruct_failure_streak(agentic_dir, scheduler))
    stages["worktree_compatibility_validation"] = _stage(
        "worktree_compatibility_validation",
        _worktree_compatibility_validation(agentic_dir))

    if log:
        log({"event": "recovery_pipeline_run",
            "stages": {k: v.get("count", 0) for k, v in stages.items()}})
    return stages
