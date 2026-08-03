"""Canonical-contract-divergence recovery (item 8).

Before items 1-5 of this fix existed, a task's backlog `expected_paths`
could silently diverge from what the conductor's work order actually
required the worker to produce -- the bootstrap validator only ever
checked the backlog's (narrower) list, so a worker that produced exactly
what the work order asked for (e.g. `.gitignore`, a typed `src`
directory, `src/index.js`, a typed `tests` directory) could still have
its own "blocked" claim about missing capabilities copied verbatim into
a `code=None` blocker (see `core.fscap.contradicted_capability_claim` and
`core.project.py`'s preflight/coder-blocked handling, which now close
this gap for every FUTURE attempt).

This module migrates a task stuck on that now-fixed defect: the backlog
task's `expected_paths` gets every typed output it should always have
declared, the stale blocker is resolved (never merely deleted -- resolved
records stay as labelled historical evidence), its memory record is
superseded (never re-presented as a live constraint), attempts
attributable to the defect are reset, and only the affected task's own
prompt/context-cache entries are invalidated."""
import datetime as _dt
import glob
import json
import os
import re

from . import bootstrap_gate, cachestore, contract as contract_mod, projstate

# The exact wording the historical bug produced (either the bootstrap
# structural gate's own evidence text, or a worker's self-reported
# "blocked" claim later found to be a contradicted capability claim --
# see core.fscap). Anchored loosely enough to survive truncation, never
# so loose it matches an unrelated blocker.
_LEGACY_CONTRACT_DIVERGENCE_RE = re.compile(
    r"bootstrap-expected-paths validation|"
    r"director(y|ies) requires? mkdir|require mkdir/git init|"
    r"contradicted capability claim", re.I)

# The six typed required outputs item 1 specifies for ollama-pilot's
# t1-init-repo -- the backlog's canonical declaration was always missing
# three of them (.gitignore, src/index.js, tests); package.json,
# index.html, and the src directory were already correctly declared and
# are simply retained.
OLLAMA_PILOT_T1_INIT_REPO_ENTRIES = (
    {"path": "package.json", "type": "file", "required": True,
     "non_empty": True},
    {"path": "index.html", "type": "file", "required": True,
     "non_empty": True},
    {"path": ".gitignore", "type": "file", "required": True,
     "non_empty": True},
    {"path": "src", "type": "directory", "required": True,
     "non_empty": False},
    {"path": "src/index.js", "type": "file", "required": True,
     "non_empty": True},
    {"path": "tests", "type": "directory", "required": True,
     "non_empty": False},
)

# Built-in migrations for known projects -- the ONLY generic-recovery
# fallback used when a caller doesn't supply `add_entries` explicitly.
_KNOWN_TASK_MIGRATIONS = {
    "t1-init-repo": OLLAMA_PILOT_T1_INIT_REPO_ENTRIES,
}


def is_legacy_contract_divergence_blocker(blocker):
    """True only for the exact pre-fix shape: `code` is None AND the
    reason matches one of the known historical signatures this specific
    defect produced. Never a bare substring match on an unrelated word."""
    return (blocker.get("code") is None and
           bool(_LEGACY_CONTRACT_DIVERGENCE_RE.search(
                (blocker.get("reason") or "").strip())))


def _supersede_blocker_memory(cfg, memory_dir, blocker, resolution_summary):
    """Item 6: the `failed_attempt` memory record behind a resolved
    blocker must never be served again as if it were a live constraint --
    write a `resolution` record that supersedes it (memsvc's own
    `supersedes` mechanism flips the old record to `status=superseded`,
    which `memory_items`'s active-only default already excludes from
    prompt injection)."""
    record_id = blocker.get("memory_record_id")
    if not record_id:
        return None
    try:
        from . import memsvc
        service = memsvc.get_memory(cfg, memory_dir)
        return service.save(
            "resolution",
            "resolved: %s" % (blocker.get("reason") or "")[:200],
            resolution_summary, task_id=blocker.get("task"),
            supersedes=record_id, importance=0.6)
    except Exception:   # noqa: BLE001 -- memory hygiene is best-effort
        return None


def migrate_task_contract(agentic_dir, memory_dir, cfg, task_id,
                          add_entries):
    """Merge every entry in `add_entries` (typed, backlog-shaped) into
    `task_id`'s `expected_paths` -- idempotent: an entry whose `path`
    already exists is left untouched, never duplicated. Returns the list
    of entries actually added (empty on a second run)."""
    backlog = {t["id"]: t for t in projstate.load_backlog(agentic_dir)}
    task = backlog.get(task_id)
    if task is None:
        return []
    existing = [bootstrap_gate.normalize_expected_entry(e)
               for e in task.get("expected_paths") or []]
    existing_paths = {e["path"] for e in existing}
    added = []
    for raw in add_entries:
        entry = bootstrap_gate.normalize_expected_entry(raw)
        if entry["path"] in existing_paths:
            continue
        existing.append(entry)
        existing_paths.add(entry["path"])
        added.append(entry)
    if added:
        projstate.update_task(agentic_dir, task_id,
                              expected_paths=existing, contract_hash=None)
        # invalidate THIS task's cache entries right now (item 6) --
        # never waits for a later cycle to notice the hash changed, and
        # never touches another task's entries (dependency name is
        # task-scoped). The exact new hash isn't known yet (the work
        # order that will produce it doesn't exist until the next
        # conductor call), so a sentinel guaranteed to differ from
        # whatever was previously recorded is enough to force it.
        store = cachestore.CacheStore(memory_dir)
        store.invalidate_dependents(
            "task_contract_hash:%s" % task_id,
            "migrated:" + _dt.datetime.now().isoformat(),
            reason="canonical-contract-divergence migration for %s"
            % task_id)
    return added



def _fixed_comparison_artifact(agentic_dir, task_id):
    """Return immutable evidence only when the saved contract now passes.

    This recovers false structural mismatches caused by the old comparison
    treating explanatory work-order prose as a literal path. It never edits
    the canonical contract and never clears a genuinely divergent artifact.
    """
    runs_root = os.path.join(str(agentic_dir), "runs")
    cycle_dirs = sorted(
        glob.glob(os.path.join(runs_root, "cycle-*")),
        key=lambda p: os.path.getmtime(p), reverse=True)
    for cycle_dir in cycle_dirs:
        path = os.path.join(cycle_dir, "task-contract.json")
        try:
            with open(path, encoding="utf-8") as fh:
                contract = json.load(fh)
        except (OSError, ValueError):
            continue
        if contract.get("task_id") != task_id:
            continue
        if not contract_mod.find_work_order_divergences(contract):
            return {
                "run_id": contract.get("run_id") or
                          os.path.basename(cycle_dir).replace("cycle-", "", 1),
                "evidence_ref": path,
            }
        return None
    return None


def recover_fixed_contract_comparison_blockers(agentic_dir, memory_dir, cfg):
    """Reset a structural blocker proven false by its saved contract artifact."""
    if not projstate.exists(agentic_dir):
        return []
    backlog = {t["id"]: t for t in projstate.load_backlog(agentic_dir)}
    blockers_doc = projstate.read_yaml(agentic_dir, "blockers.yaml",
                                       {"blockers": []})
    events = []
    changed = False
    for blocker in blockers_doc.get("blockers", []):
        if blocker.get("resolved") or blocker.get("code") != \
                projstate.BLOCKER_CODE_STRUCTURAL_CONTRACT_MISMATCH:
            continue
        task_id = blocker.get("task")
        task = backlog.get(task_id)
        if task is None or task.get("status") != "blocked":
            continue
        evidence = _fixed_comparison_artifact(agentic_dir, task_id)
        if not evidence:
            continue
        blocker.update({
            "resolved": True,
            "resolved_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "failure_class": "task_contract_invalid",
            "platform_owned": True,
            "retryable": True,
            "evidence_ref": evidence["evidence_ref"],
        })
        projstate.update_task(agentic_dir, task_id, status="pending",
                              blocking_reason=None, last_result=None,
                              attempts=0)
        memory_event = _supersede_blocker_memory(
            cfg, memory_dir, blocker,
            "false structural-contract mismatch repaired: saved contract "
            "artifact now validates under prose-aware output comparison; "
            "task reset to pending, never marked complete")
        events.append({
            "task_id": task_id,
            "action": "reset_false_structural_contract_mismatch",
            "resolved_blockers": 1,
            "memory_superseded": bool(memory_event),
            **evidence,
        })
        changed = True
    if changed:
        projstate.write_yaml(agentic_dir, "blockers.yaml", blockers_doc)
    return events




def _conductor_expansion_artifact(agentic_dir, task_id):
    """Find a saved contract blocked only by conductor output expansion."""
    runs_root = os.path.join(str(agentic_dir), "runs")
    cycle_dirs = sorted(
        glob.glob(os.path.join(runs_root, "cycle-*")),
        key=lambda p: os.path.getmtime(p), reverse=True)
    for cycle_dir in cycle_dirs:
        path = os.path.join(cycle_dir, "task-contract.json")
        try:
            with open(path, encoding="utf-8") as fh:
                contract = json.load(fh)
        except (OSError, ValueError):
            continue
        if contract.get("task_id") != task_id:
            continue
        divergences = contract_mod.find_work_order_divergences(contract)
        if divergences:
            return {
                "run_id": contract.get("run_id") or
                          os.path.basename(cycle_dir).replace("cycle-", "", 1),
                "evidence_ref": path,
                "discarded_conductor_outputs":
                    contract.get("work_order_expected_outputs") or [],
                "canonical_required_outputs": [
                    entry.get("path")
                    for entry in contract.get("required_outputs") or []],
            }
        return None
    return None


def recover_stable_contract_authority_blockers(
        agentic_dir, memory_dir, cfg):
    """Reset pre-fix blockers caused by conductor-added expected outputs.

    Stable authority now deterministically projects every future work order
    back onto backlog expected_paths. Retrying is therefore safe; recovery
    never adds the conductor's proposal to the contract and never marks work
    complete.
    """
    if not projstate.exists(agentic_dir):
        return []
    backlog = {task["id"]: task for task in
               projstate.load_backlog(agentic_dir)}
    blockers_doc = projstate.read_yaml(agentic_dir, "blockers.yaml",
                                       {"blockers": []})
    events = []
    changed = False
    for blocker in blockers_doc.get("blockers", []):
        if blocker.get("resolved") or blocker.get("code") != \
                projstate.BLOCKER_CODE_STRUCTURAL_CONTRACT_MISMATCH:
            continue
        reason = blocker.get("reason") or ""
        if "work-order expected output" not in reason or \
                "required_outputs" not in reason:
            continue
        task_id = blocker.get("task")
        task = backlog.get(task_id)
        if task is None or task.get("status") != "blocked":
            continue
        evidence = _conductor_expansion_artifact(agentic_dir, task_id)
        if not evidence:
            continue
        blocker.update({
            "resolved": True,
            "resolved_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "failure_class": "task_contract_invalid",
            "platform_owned": True,
            "retryable": True,
            "evidence_ref": evidence["evidence_ref"],
        })
        projstate.update_task(agentic_dir, task_id, status="pending",
                              blocking_reason=None, last_result=None,
                              attempts=0)
        memory_event = _supersede_blocker_memory(
            cfg, memory_dir, blocker,
            "stable contract authority enabled: conductor-added outputs "
            "discarded; backlog contract remains authoritative; task reset "
            "to pending, never marked complete")
        events.append({
            "task_id": task_id,
            "action": "reset_conductor_contract_expansion",
            "resolved_blockers": 1,
            "memory_superseded": bool(memory_event),
            **evidence,
        })
        changed = True
    if changed:
        projstate.write_yaml(agentic_dir, "blockers.yaml", blockers_doc)
    return events
def recover_contract_divergence_blockers(agentic_dir, memory_dir, cfg,
                                         task_entries=None):
    """Self-heal for tasks blocked on the legacy canonical-contract-
    divergence defect (item 8). `task_entries` maps task_id -> the typed
    entries to merge into its `expected_paths`; tasks not present in the
    map fall back to `_KNOWN_TASK_MIGRATIONS` (built-in, e.g.
    ollama-pilot's t1-init-repo) and are otherwise skipped -- this never
    guesses what a task's canonical contract SHOULD have been. Never
    marks a task done, only resets it to pending; genuinely unrelated
    blockers (a different reason, or an already-resolved one) are left
    exactly as they were."""
    if not projstate.exists(agentic_dir):
        return []
    task_entries = dict(task_entries or {})
    backlog = {t["id"]: t for t in projstate.load_backlog(agentic_dir)}
    blockers_doc = projstate.read_yaml(agentic_dir, "blockers.yaml",
                                       {"blockers": []})
    events = []
    changed = False
    for b in blockers_doc.get("blockers", []):
        if b.get("resolved") or not is_legacy_contract_divergence_blocker(b):
            continue
        task_id = b.get("task")
        task = backlog.get(task_id)
        entries = task_entries.get(task_id) or \
            _KNOWN_TASK_MIGRATIONS.get(task_id)
        if task is None or task.get("status") != "blocked" or not entries:
            continue
        added = migrate_task_contract(agentic_dir, memory_dir, cfg, task_id,
                                      entries)
        projstate.update_task(agentic_dir, task_id, status="pending",
                              blocking_reason=None,
                              # attempts recorded so far were all against
                              # the divergent (broken) contract -- never
                              # attributable to the model/provider once
                              # the contract itself is what was wrong.
                              attempts=0)
        b["resolved"] = True
        b["resolved_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        b["code"] = projstate.BLOCKER_CODE_STRUCTURAL_CONTRACT_MISMATCH
        changed = True
        migrated_task = next(t for t in projstate.load_backlog(agentic_dir)
                            if t["id"] == task_id)
        memory_event = _supersede_blocker_memory(
            cfg, memory_dir, b,
            "canonical-contract-divergence defect repaired: %s now "
            "declares %d typed required output(s); task reset to "
            "pending, never marked complete"
            % (task_id, len(migrated_task["expected_paths"])))
        events.append({"task_id": task_id, "added_entries": added,
                       "memory_superseded": bool(memory_event)})
    if changed:
        projstate.write_yaml(agentic_dir, "blockers.yaml", blockers_doc)
    return events
