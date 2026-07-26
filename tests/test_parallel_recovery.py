"""Fix for the aggregate Phase-5 "parallel candidates exhausted" blocker:
before this fix, every candidate being disqualified collapsed into one
free-form, concatenated-reason blocker with `code=None` -- unrecognisable
to any recovery routine, and permanently stuck even after the underlying
(per-candidate) platform contract defects were fixed. This covers:
structured candidate_failures + aggregate classification (item 1/2),
legacy blocker migration for the exact persisted ollama-pilot shape
(item 3), and the resulting task/dependency/worktree recovery (item 5)."""
import os

from conftest import Clock, FakeCaller, project_cfg, proj_order, seed_project, simple_task, worker_out
from core import failures, parallel_recovery, projstate, taskspace
from core.project import run_cycle

OLLAMA_PILOT_REASON = (
    "parallel candidates exhausted: all 2 candidate(s) disqualified "
    "(t1-init-repo: .gitignore is in allowed_paths but rejected by "
    "bootstrap-expected-paths validation; src/ and tests/ directory "
    "creation req")


# -- classify_aggregate: platform / model / capacity / mixed / human -----------

def _cause(candidate_id, failure_class, code, platform_owned, retryable=True):
    return {"candidate_id": candidate_id, "failure_class": failure_class,
           "code": code, "platform_owned": platform_owned,
           "retryable": retryable, "evidence_ref": None, "detail": "x"}


def test_classify_aggregate_all_platform_owned():
    causes = [
        _cause("t1", failures.TASK_CONTRACT_INVALID,
              "bootstrap_expected_paths_contract", True),
        _cause("t1--c2", failures.TASK_CONTRACT_INVALID,
              "bootstrap_expected_paths_contract", True),
    ]
    verdict = parallel_recovery.classify_aggregate(causes)
    assert verdict["platform_owned"] is True
    assert verdict["failure_class"] == failures.TASK_CONTRACT_INVALID
    assert verdict["block"] is True
    assert verdict["human_only"] is False


def test_classify_aggregate_all_model_caused():
    causes = [
        _cause("t1", failures.DETERMINISTIC_CHECK_FAILED,
              "candidate_deterministic_check_failed", False),
        _cause("t1--c2", failures.MODEL_OUTPUT_INVALID,
              "candidate_coder_blocked", False),
    ]
    verdict = parallel_recovery.classify_aggregate(causes)
    assert verdict["platform_owned"] is False
    assert verdict["failure_class"] in (failures.DETERMINISTIC_CHECK_FAILED,
                                        failures.MODEL_OUTPUT_INVALID)
    assert verdict["block"] is True
    assert verdict["human_only"] is False


def test_classify_aggregate_all_provider_capacity_defers_not_blocks():
    causes = [
        _cause("t1", failures.PROVIDER_CAPACITY,
              "candidate_provider_capacity", False),
        _cause("t1--c2", failures.PROVIDER_CAPACITY,
              "candidate_provider_capacity", False),
    ]
    verdict = parallel_recovery.classify_aggregate(causes)
    assert verdict["failure_class"] == failures.PROVIDER_CAPACITY
    assert verdict["platform_owned"] is False
    assert verdict["block"] is False   # capacity deferral, never a hard block


def test_classify_aggregate_mixed_causes_preserved_and_precedence_applied():
    causes = [
        _cause("t1", failures.TASK_CONTRACT_INVALID,
              "bootstrap_expected_paths_contract", True),
        _cause("t1--c2", failures.MODEL_OUTPUT_INVALID,
              "candidate_coder_blocked", False),
    ]
    verdict = parallel_recovery.classify_aggregate(causes)
    # platform-owned causes outrank plain model causes in the documented
    # precedence -- but this is NOT the "all platform" branch (mixed)
    assert verdict["failure_class"] == failures.TASK_CONTRACT_INVALID
    assert verdict["code"] == "parallel_candidates_exhausted_mixed"
    assert verdict["block"] is True


def test_classify_aggregate_genuine_human_decision_wins_any_mix():
    causes = [
        _cause("t1", failures.GENUINE_HUMAN_DECISION,
              "candidate_human", False, retryable=False),
        _cause("t1--c2", failures.DETERMINISTIC_CHECK_FAILED,
              "candidate_deterministic_check_failed", False),
    ]
    verdict = parallel_recovery.classify_aggregate(causes)
    assert verdict["human_only"] is True
    assert verdict["failure_class"] == failures.GENUINE_HUMAN_DECISION


def test_classify_aggregate_empty_candidate_failures_is_safe():
    verdict = parallel_recovery.classify_aggregate([])
    assert verdict["failure_class"] is None
    assert verdict["block"] is True


# -- legacy blocker recognition: exact prefix only, never bare "parallel" ------

def test_legacy_blocker_recognised_by_exact_prefix():
    blocker = {"code": None, "reason": OLLAMA_PILOT_REASON}
    assert parallel_recovery.is_legacy_parallel_exhausted_blocker(blocker)


def test_unrelated_parallel_blocker_never_matches_bare_word():
    blocker = {"code": None,
              "reason": "task involves parallel processing logic; "
                        "conductor queued for architecture review"}
    assert not parallel_recovery.is_legacy_parallel_exhausted_blocker(blocker)


def test_blocker_with_explicit_code_is_never_legacy_even_if_wording_matches():
    blocker = {"code": "some_other_code", "reason": OLLAMA_PILOT_REASON}
    assert not parallel_recovery.is_legacy_parallel_exhausted_blocker(blocker)


# -- legacy reconstruction: exact ollama-pilot text -----------------------------

def test_reconstruct_legacy_candidate_failures_from_exact_ollama_pilot_text():
    causes = parallel_recovery.reconstruct_legacy_candidate_failures(
        OLLAMA_PILOT_REASON)
    assert len(causes) == 2
    assert all(c["platform_owned"] for c in causes)
    assert causes[0]["code"] == "bootstrap_expected_paths_contract"
    assert causes[0]["failure_class"] == failures.TASK_CONTRACT_INVALID
    assert causes[1]["code"] == "bootstrap_missing_scaffold_directories"


def test_reconstruct_legacy_candidate_failures_never_fabricates_unknown_cause():
    causes = parallel_recovery.reconstruct_legacy_candidate_failures(
        "all 2 candidate(s) disqualified (t1: some genuinely new model "
        "problem never seen before; t1--c2: another new problem)")
    assert all(not c["platform_owned"] for c in causes)
    assert all(c["failure_class"] is None for c in causes)


# -- recover_parallel_candidate_aggregate: exact ollama-pilot migration ---------

def test_exact_ollama_pilot_blocker_migration_resets_task_to_pending(sandbox):
    project_cfg(sandbox)
    task = simple_task("t1-init-repo", kind="bootstrap")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    projstate.update_task(a, "t1-init-repo", status="blocked",
                          blocking_reason=OLLAMA_PILOT_REASON)
    projstate.add_blocker(a, "t1-init-repo", OLLAMA_PILOT_REASON,
                          human_only=False, code=None)

    events = parallel_recovery.recover_parallel_candidate_aggregate(a)
    assert len(events) == 1
    assert events[0]["task_id"] == "t1-init-repo"
    assert events[0]["recovered"] is True

    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t1-init-repo"]["status"] == "pending"
    assert tasks["t1-init-repo"]["blocking_reason"] is None
    assert tasks["t1-init-repo"]["status"] != "done"   # never marked complete

    blockers = projstate.read_yaml(a, "blockers.yaml")["blockers"]
    assert blockers[0]["resolved"] is True
    assert blockers[0]["code"] == \
        projstate.BLOCKER_CODE_PARALLEL_CANDIDATES_EXHAUSTED
    assert blockers[0]["resolved_at"] is not None
    assert blockers[0]["candidate_failures"][0]["platform_owned"] is True


def test_incompatible_preserved_candidate_worktrees_reverted(sandbox):
    project_cfg(sandbox)
    task = simple_task("t1-init-repo", kind="bootstrap")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    root = str(sandbox["repo"])
    from core.project import PROJECT_BRANCH, ensure_project_worktree
    ensure_project_worktree(sandbox["cfg"], {
        "agentic": a, "root": root,
        "memory": str(sandbox["agentic"] / "memory"),
        "queue": str(sandbox["agentic"] / "queue"),
        "runs": str(sandbox["agentic"] / "runs")})
    path = taskspace.create_task_worktree(root, a, "t1-init-repo",
                                          PROJECT_BRANCH)
    stale = os.path.join(path, "stale.gitignore")
    with open(stale, "w", encoding="utf-8") as fh:
        fh.write("node_modules/\n")
    from conftest import git
    git(["add", "-A"], path)
    assert "stale.gitignore" in os.listdir(path)

    projstate.update_task(a, "t1-init-repo", status="blocked",
                          blocking_reason=OLLAMA_PILOT_REASON)
    projstate.add_blocker(a, "t1-init-repo", OLLAMA_PILOT_REASON,
                          human_only=False, code=None)
    events = parallel_recovery.recover_parallel_candidate_aggregate(a)
    assert "t1-init-repo" in events[0]["worktrees_reverted"]
    assert "stale.gitignore" not in os.listdir(path)


def test_unrelated_parallel_blocker_stays_unresolved_after_recovery(sandbox):
    project_cfg(sandbox)
    task = simple_task("t1-first")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    reason = ("task involves parallel processing logic; conductor queued "
             "for architecture review")
    projstate.update_task(a, "t1-first", status="blocked",
                          blocking_reason=reason)
    projstate.add_blocker(a, "t1-first", reason, human_only=False, code=None)

    events = parallel_recovery.recover_parallel_candidate_aggregate(a)
    assert events == []
    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t1-first"]["status"] == "blocked"
    blockers = projstate.read_yaml(a, "blockers.yaml")["blockers"]
    assert blockers[0]["resolved"] is False


def test_genuine_completed_work_is_preserved_during_recovery(sandbox):
    """Recovery only ever touches the task actually named by a matching
    legacy aggregate blocker -- a `done` task (genuine completed work)
    and an unrelated `pending` task must come out exactly as they went
    in, even though they share the same recovery pass."""
    project_cfg(sandbox)
    t1 = simple_task("t1-init-repo", kind="bootstrap")
    t2 = simple_task("t2-done", status="done", last_result="pass")
    t3 = simple_task("t3-pending")
    seed_project(sandbox, [t1, t2, t3])
    a = str(sandbox["agentic"])
    projstate.update_task(a, "t1-init-repo", status="blocked",
                          blocking_reason=OLLAMA_PILOT_REASON)
    projstate.add_blocker(a, "t1-init-repo", OLLAMA_PILOT_REASON,
                          human_only=False, code=None)

    parallel_recovery.recover_parallel_candidate_aggregate(a)

    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t2-done"]["status"] == "done"
    assert tasks["t2-done"]["last_result"] == "pass"
    assert tasks["t3-pending"]["status"] == "pending"
    assert tasks["t1-init-repo"]["status"] == "pending"   # recovered
    assert tasks["t1-init-repo"]["status"] != "done"       # never completed


def test_mixed_cause_legacy_blocker_is_not_auto_recovered(sandbox):
    """One recognised platform signature plus one genuinely unrecognised
    cause must NOT be treated as "all platform-owned" -- the task stays
    blocked rather than being silently unblocked on partial evidence."""
    project_cfg(sandbox)
    task = simple_task("t1-mixed", kind="bootstrap")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    reason = ("parallel candidates exhausted: all 2 candidate(s) "
             "disqualified (t1-mixed: .gitignore is in allowed_paths but "
             "rejected by bootstrap-expected-paths validation; "
             "t1-mixed--c2: a brand new, never-seen model failure)")
    projstate.update_task(a, "t1-mixed", status="blocked",
                          blocking_reason=reason)
    projstate.add_blocker(a, "t1-mixed", reason, human_only=False, code=None)

    events = parallel_recovery.recover_parallel_candidate_aggregate(a)
    assert events[0]["recovered"] is False
    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t1-mixed"]["status"] == "blocked"


def test_dependency_unblocked_after_aggregate_recovery(sandbox):
    project_cfg(sandbox)
    t1 = simple_task("t1-init-repo", kind="bootstrap")
    m1 = simple_task("m1-scaffold", dependencies=["t1-init-repo"])
    seed_project(sandbox, [t1, m1])
    a = str(sandbox["agentic"])
    projstate.update_task(a, "t1-init-repo", status="blocked",
                          blocking_reason=OLLAMA_PILOT_REASON)
    projstate.add_blocker(a, "t1-init-repo", OLLAMA_PILOT_REASON,
                          human_only=False, code=None)
    # before recovery: permanently stuck, nothing is ever dispatchable
    assert projstate.next_task(a) is None

    parallel_recovery.recover_parallel_candidate_aggregate(a)
    # t1 is reachable again; m1-scaffold's path is restored (it will
    # become eligible once t1 actually completes, never before)
    next_up = projstate.next_task(a)
    assert next_up["id"] == "t1-init-repo"

    projstate.update_task(a, "t1-init-repo", status="done")
    next_up = projstate.next_task(a)
    assert next_up["id"] == "m1-scaffold"


# -- new-format (forward-looking) aggregate: structured, never code=None ------

def qa_pass():
    return {"verdict": "pass", "done_when_results": [], "reason": "ok",
           "out_of_scope_changes": [], "test_integrity_preserved": True}


def sec_pass():
    return {"verdict": "pass", "concerns": [], "reason": "clean"}


def test_new_aggregate_all_candidates_disqualified_never_produces_code_none(
        sandbox):
    """The root-cause fix: a FRESH "all candidates disqualified" outcome
    must never persist a code=None blocker -- every new aggregate is
    structured and classified (items 1/2/8)."""
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = [
        {"name": "fails", "command": "python -c \"import sys; sys.exit(1)\"",
         "mandatory": True}]
    task = simple_task(risk="high", deterministic_checks=[])
    seed_project(sandbox, [task])
    caller = FakeCaller({
        "conductor": proj_order(task),
        "coder": [worker_out(edits=[{"path": "src/app.py", "action": "write",
                                     "content": "VALUE = 2\n"}]),
                 worker_out(edits=[{"path": "src/app.py", "action": "write",
                                    "content": "VALUE = 3\n"}])],
        "qa": qa_pass(), "security": sec_pass()})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "failure"
    a = str(sandbox["agentic"])
    blockers = projstate.open_blockers(a)
    assert blockers
    blocker = blockers[0]
    assert blocker["code"] == \
        projstate.BLOCKER_CODE_PARALLEL_CANDIDATES_EXHAUSTED
    assert blocker["failure_class"] is not None
    assert blocker["candidate_failures"]
    assert all(cf["failure_class"] for cf in blocker["candidate_failures"])
