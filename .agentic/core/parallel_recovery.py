"""Aggregate parallel-candidate blocker: classification + recovery.

Phase 5 (`parallel.py`) can disqualify every candidate for a task, in
which case `project.py` must record ONE aggregate blocker. Before this
module existed, that aggregate was a free-form concatenated string with
`code=None` -- unrecognisable to any recovery routine, and permanently
stuck even after the underlying (per-candidate) defects were fixed. This
module:

1. classifies a fresh aggregate from its STRUCTURED constituent causes
   (`classify_aggregate`, fed by `parallel.run_candidates`'s
   `candidate_failures` -- never by re-parsing a reason string), and
2. migrates the historical code=None shape by reconstructing candidate
   causes from the persisted free text, ONLY for the exact legacy
   wording produced by the fixed bug (never merely because a blocker's
   reason contains the word "parallel" -- see
   `is_legacy_parallel_exhausted_blocker`).

A recovered task is always reset to pending, NEVER marked done -- the
work itself was never attempted successfully; only the platform's own
contract/capability defect that wrongly disqualified every candidate is
what gets cleared.
"""
import datetime as _dt
import os
import re

from . import failures, projstate

# -- aggregate classification (fresh, structured candidate_failures) -----------

# Precedence used only when candidate causes are genuinely MIXED (not all
# the same class) -- highest-priority cause present decides the policy,
# while every individual cause stays visible in candidate_failures for
# evidence. Never a guess: a class only appears here if at least one
# candidate was actually classified with it.
_PRECEDENCE = (
    failures.GENUINE_HUMAN_DECISION,
    failures.PROVIDER_AUTHENTICATION,
    failures.TASK_CONTRACT_INVALID,
    failures.PLATFORM_CAPABILITY_MISSING,
    failures.WORKSPACE_POLICY_DENIED,
    failures.PROJECT_DEPENDENCY_MISSING,
    failures.PROVIDER_CAPACITY,
    failures.PROVIDER_UNAVAILABLE,
    failures.EXECUTION_TIMEOUT,
    failures.DETERMINISTIC_CHECK_FAILED,
    failures.MODEL_OUTPUT_INVALID,
    failures.INFRASTRUCTURE_FAILURE,
)


def classify_aggregate(candidate_failures):
    """Classify an "all candidates disqualified" outcome from its
    constituent, structured causes (item 2). Returns a dict:
    `failure_class`, `code` (a stable sub-classification, informational --
    the persisted BLOCKER code is always
    `projstate.BLOCKER_CODE_PARALLEL_CANDIDATES_EXHAUSTED`), `platform_owned`
    (the enacted POLICY's platform-relatedness -- may be True for a mixed
    aggregate whose highest-precedence cause happens to be platform-owned),
    `all_platform_owned` (strict: True only when EVERY candidate cause is
    platform-owned -- this, never `platform_owned`, is what recovery gates
    an automatic unblock on), `retryable`, `human_only`, `block` (whether
    the task should actually be blocked, vs. left pending for an automatic
    retry -- e.g. a pure capacity deferral never blocks), and `reason`."""
    all_platform_owned = bool(candidate_failures) and all(
        cf.get("platform_owned") for cf in candidate_failures)
    if not candidate_failures:
        return {"failure_class": None, "code": None, "platform_owned": False,
                "all_platform_owned": False, "retryable": True,
                "human_only": False, "block": True,
                "reason": "no structured candidate failures were recorded"}
    classes = [cf.get("failure_class") for cf in candidate_failures]
    if any(c == failures.GENUINE_HUMAN_DECISION for c in classes):
        return {"failure_class": failures.GENUINE_HUMAN_DECISION,
                "code": "parallel_candidates_exhausted_human",
                "platform_owned": False, "all_platform_owned": False,
                "retryable": False, "human_only": True, "block": True,
                "reason": "at least one candidate genuinely requires a "
                         "human decision"}
    if all_platform_owned:
        common = classes[0] if len(set(classes)) == 1 \
            else failures.TASK_CONTRACT_INVALID
        return {"failure_class": common,
                "code": "parallel_candidates_exhausted_platform",
                "platform_owned": True, "all_platform_owned": True,
                "retryable": True, "human_only": False, "block": True,
                "reason": "every candidate was disqualified by a "
                         "platform-owned cause"}
    if all(c == failures.PROVIDER_CAPACITY for c in classes):
        return {"failure_class": failures.PROVIDER_CAPACITY, "code": None,
                "platform_owned": False, "all_platform_owned": False,
                "retryable": True, "human_only": False, "block": False,
                "reason": "every candidate was disqualified by provider "
                         "capacity -- deferred, not blocked"}
    if all(c in (failures.DETERMINISTIC_CHECK_FAILED,
                failures.MODEL_OUTPUT_INVALID) for c in classes):
        common = classes[0] if len(set(classes)) == 1 \
            else failures.MODEL_OUTPUT_INVALID
        return {"failure_class": common,
                "code": "parallel_candidates_exhausted_model",
                "platform_owned": False, "all_platform_owned": False,
                "retryable": True, "human_only": False, "block": True,
                "reason": "every candidate failed for model/"
                         "deterministic-check reasons"}
    # mixed causes: preserve every cause (candidate_failures, untouched),
    # choose the enacted policy by explicit precedence only. Never
    # all_platform_owned here by construction (that branch already
    # returned above) -- `platform_owned` may still be True (the chosen
    # policy is platform-related) without ever being treated as grounds
    # for an automatic unblock.
    for cls in _PRECEDENCE:
        if cls in classes:
            platform_owned = any(
                cf.get("platform_owned") for cf in candidate_failures
                if cf.get("failure_class") == cls)
            return {"failure_class": cls,
                    "code": "parallel_candidates_exhausted_mixed",
                    "platform_owned": platform_owned,
                    "all_platform_owned": False, "retryable": True,
                    "human_only": False, "block": True,
                    "reason": "mixed candidate causes; policy chosen by "
                             "precedence (%s)" % cls}
    return {"failure_class": failures.MODEL_OUTPUT_INVALID,
            "code": "parallel_candidates_exhausted_mixed",
            "platform_owned": False, "all_platform_owned": False,
            "retryable": True, "human_only": False, "block": True,
            "reason": "mixed candidate causes; no recognised class matched"}


# -- legacy migration (free-form reason text, code=None) -----------------------

# Anchored at the start -- never matches merely because a reason contains
# the word "parallel" somewhere (item 3, explicit requirement).
_LEGACY_PARALLEL_EXHAUSTED_RE = re.compile(
    r"^parallel candidates exhausted:", re.I)

# The four known, already-fixed platform contract defects that produced
# the historical aggregate blocker (see the Phase 5 delivery report and
# core.bootstrap_gate). Each pattern is written to tolerate truncation
# (persisted reasons are cut at 200/300 chars) -- matched as a substring
# fragment, never requiring the full historical sentence.
_LEGACY_GITIGNORE_EXPECTED_PATHS_RE = re.compile(
    r"\.gitignore is in allowed_paths but rejected by "
    r"bootstrap-expected-paths validation", re.I)
_LEGACY_MISSING_SCAFFOLD_DIR_RE = re.compile(
    r"(src|tests?)[/\\]?\s*(and\s+(src|tests?)[/\\]?\s*)?directory creation "
    r"req", re.I)
_LEGACY_STALE_WORKTREE_RE = re.compile(
    r"outside task.?s expected_paths|expected_paths.{0,40}despite.{0,40}"
    r"allowed_paths|forbidden path .{0,80} touched|"
    r"cannot edit paths outside allowed_paths", re.I)
_LEGACY_FALSE_CAPABILITY_RE = re.compile(
    r"worker role cannot|fabricat\w* .{0,20}capabilit|"
    r"invent\w* .{0,20}capabilit", re.I)

_LEGACY_SIGNATURES = (
    ("bootstrap_expected_paths_contract", failures.TASK_CONTRACT_INVALID,
     _LEGACY_GITIGNORE_EXPECTED_PATHS_RE),
    ("bootstrap_missing_scaffold_directories", failures.TASK_CONTRACT_INVALID,
     _LEGACY_MISSING_SCAFFOLD_DIR_RE),
    ("stale_preserved_candidate_worktree", failures.TASK_CONTRACT_INVALID,
     _LEGACY_STALE_WORKTREE_RE),
    ("conductor_false_capability_limitation",
     failures.PLATFORM_CAPABILITY_MISSING, _LEGACY_FALSE_CAPABILITY_RE),
)


def legacy_platform_signature(text):
    """(code, failure_class) for the first known already-fixed platform
    defect signature found in `text`, or None. Shared with
    `core.recovery`'s failure-streak reconstruction so a historical
    cycle-log entry can be retroactively recognised as platform-caused
    using the exact same evidence this module's blocker migration uses."""
    text = text or ""
    for code, failure_class, pattern in _LEGACY_SIGNATURES:
        if pattern.search(text):
            return code, failure_class
    return None


def is_legacy_parallel_exhausted_blocker(blocker):
    """True only for the exact pre-fix aggregate shape: `code` is None AND
    the reason is the "parallel candidates exhausted: ..." wrapper this
    module's call site in project.py produced before it passed a `code`/
    `failure_class`. Never matches on the bare word "parallel" alone."""
    return (blocker.get("code") is None and
           bool(_LEGACY_PARALLEL_EXHAUSTED_RE.search(
                (blocker.get("reason") or "").strip())))


_SEGMENT_PREFIX_RE = re.compile(r"^\s*([\w./\-]+):\s*(.*)$", re.S)


def _split_candidate_segments(reasoning_text):
    """Best-effort split of the concatenated per-candidate reasoning
    (`"; ".join("%s: %s" % (candidate_id, reason) ...)` -- see
    `parallel.select_winner`) back into (candidate_id_or_None, text)
    segments. Persisted text may be truncated mid-word; this never
    assumes a clean trailing delimiter."""
    match = re.search(r"disqualified \((.*)\)?\s*$", reasoning_text, re.S)
    body = match.group(1) if match else reasoning_text
    segments = [s.strip() for s in body.split(";") if s.strip()]
    out = []
    for seg in segments:
        seg_match = _SEGMENT_PREFIX_RE.match(seg)
        if seg_match:
            out.append((seg_match.group(1), seg_match.group(2)))
        else:
            out.append((None, seg))
    return out or [(None, reasoning_text)]


def reconstruct_legacy_candidate_failures(reasoning_text):
    """Structured `candidate_failures` reconstructed from a legacy
    concatenated reason string (item 3/5) -- ONLY the segments matching a
    known, already-fixed platform signature are marked platform_owned;
    an unrecognised segment stays unclassified (never fabricated as
    platform-owned), so `classify_aggregate` only calls the WHOLE
    aggregate platform-only when every segment is conclusively so."""
    segments = _split_candidate_segments(reasoning_text or "")
    out = []
    for idx, (candidate_id, text) in enumerate(segments):
        cause = legacy_platform_signature(text)
        code, failure_class = cause if cause else (None, None)
        out.append({
            "candidate_id": candidate_id or "legacy-candidate-%d" % (idx + 1),
            "failure_class": failure_class, "code": code,
            "platform_owned": bool(cause), "retryable": True,
            "evidence_ref": None, "detail": text,
            "migrated_from_legacy_text": True,
        })
    return out


def _revert_candidate_worktrees(agentic_dir, task_id):
    """Best-effort revert of any preserved candidate worktree for
    `task_id` (item 5) -- `parallel.decide_agent_count` never fans out
    past 2 candidates, so `task_id` and `task_id--c2` are the only
    possible worktree names. Mirrors
    `bootstrap_gate._revert_preserved_worktree_if_present`: never raises,
    cleanup here is a courtesy, not a safety boundary."""
    from . import gitops, taskspace
    reverted = []
    for candidate_id in (task_id, "%s--c2" % task_id):
        path = taskspace.task_worktree_path(agentic_dir, candidate_id)
        if not os.path.exists(os.path.join(path, ".git")):
            continue
        gitops.run_git(["reset", "--hard", "HEAD"], cwd=path, check=False)
        gitops.run_git(["clean", "-fd"], cwd=path, check=False)
        reverted.append(candidate_id)
    return reverted


def recover_parallel_candidate_aggregate(agentic_dir):
    """Self-heal for the legacy aggregate "parallel candidates exhausted"
    blocker (items 3/5): reconstructs structured candidate_failures from
    the persisted free text, classifies the aggregate, and -- ONLY when
    every recognised cause is conclusively platform-owned -- resets the
    task to pending (never done), resolves the blocker (backfilling the
    stable code), and reverts any incompatible preserved candidate
    worktree. A mixed, model-caused, or human-required aggregate is left
    exactly as blocked as before; this never resolves a blocker merely
    because it mentions "parallel"."""
    if not projstate.exists(agentic_dir):
        return []
    backlog = {t["id"]: t for t in projstate.load_backlog(agentic_dir)}
    blockers_doc = projstate.read_yaml(agentic_dir, "blockers.yaml",
                                       {"blockers": []})
    events = []
    changed = False
    for b in blockers_doc.get("blockers", []):
        if b.get("resolved") or not is_legacy_parallel_exhausted_blocker(b):
            continue
        task_id = b.get("task")
        task = backlog.get(task_id)
        if task is None or task.get("status") != "blocked":
            continue
        candidate_failures = reconstruct_legacy_candidate_failures(
            b.get("reason") or "")
        verdict = classify_aggregate(candidate_failures)
        event = {"task_id": task_id, "candidate_failures": candidate_failures,
                 "classification": verdict, "recovered": False,
                 "worktrees_reverted": []}
        if verdict["all_platform_owned"] and not verdict["human_only"]:
            projstate.update_task(agentic_dir, task_id, status="pending",
                                  blocking_reason=None)
            b["resolved"] = True
            b["resolved_at"] = _dt.datetime.now().isoformat(
                timespec="seconds")
            b["code"] = projstate.BLOCKER_CODE_PARALLEL_CANDIDATES_EXHAUSTED
            b["failure_class"] = verdict["failure_class"]
            b["platform_owned"] = True
            b["retryable"] = True
            b["candidate_failures"] = candidate_failures
            changed = True
            event["recovered"] = True
            event["worktrees_reverted"] = _revert_candidate_worktrees(
                agentic_dir, task_id)
        events.append(event)
    if changed:
        projstate.write_yaml(agentic_dir, "blockers.yaml", blockers_doc)
    return events
