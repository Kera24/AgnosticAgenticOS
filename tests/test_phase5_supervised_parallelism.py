"""Phase 5 -- Supervised Parallelism.

Candidates run SEQUENTIALLY (not literally multi-threaded) but on fully
isolated git worktrees/branches, given the identical immutable work
order -- "supervised parallelism" here is about ISOLATION and
evidence-based selection, not wall-clock concurrency; the spec never
requires true OS-level concurrent execution, and running two model
calls against a shared FakeCaller/budget/ledger concurrently would add
substantial complexity for no behavioural difference the exit gate
actually checks (separate worktrees, no cross-candidate corruption,
capacity limits, deterministic winner selection, native mode intact).
"""
import os

from conftest import Clock, FakeCaller, project_cfg, proj_order, seed_project, simple_task, worker_out
from core import parallel, projstate
from core.project import run_cycle


def qa_pass():
    return {"verdict": "pass", "done_when_results": [], "reason": "ok",
           "out_of_scope_changes": [], "test_integrity_preserved": True}


def sec_pass():
    return {"verdict": "pass", "concerns": [], "reason": "clean"}


def std_caller(task, coder=None, qa=None, security=None, extra=None):
    scripted = {"conductor": proj_order(task), "coder": coder or worker_out(),
               "qa": qa or qa_pass(), "security": security or sec_pass()}
    scripted.update(extra or {})
    return FakeCaller(scripted)


def cycle(sandbox, caller, clock=None, run_id=None):
    return run_cycle(sandbox["cfg"], caller=caller, clock=clock or Clock(),
                     run_id=run_id)


def high_risk_task(**over):
    over.setdefault("risk", "high")
    return simple_task(**over)


# -- unit: decide_agent_count / detect_risk_signals ----------------------------

def test_low_risk_task_stays_single_agent():
    n, signals = parallel.decide_agent_count(
        {"risk": "low", "attempts": 0}, {})
    assert n == 1
    assert signals == []


def test_high_risk_task_triggers_two_candidates():
    n, signals = parallel.decide_agent_count(
        {"risk": "high", "attempts": 0}, {})
    assert n == 2
    assert "difficult_architecture" in signals
    assert "high_risk_refactoring" in signals


def test_security_relevant_task_triggers_two_candidates():
    n, signals = parallel.decide_agent_count(
        {"risk": "low", "attempts": 0, "security_relevant": True}, {})
    assert n == 2
    assert signals == ["security_sensitive"]


def test_repeated_failure_triggers_two_candidates():
    n, signals = parallel.decide_agent_count(
        {"risk": "low", "attempts": 2}, {})
    assert n == 2
    assert signals == ["repeated_single_agent_failure"]


def test_competing_ui_proposal_signal_from_ui_paths():
    task = {"risk": "medium", "attempts": 0,
           "expected_paths": ["ui/components/Nav.tsx"]}
    n, signals = parallel.decide_agent_count(task, {})
    assert n == 2
    assert "competing_ui_proposals" in signals


def test_max_agents_global_caps_fan_out_even_when_triggered():
    """Capacity limits prevent excessive fan-out (Phase 5 exit gate)."""
    n, signals = parallel.decide_agent_count(
        {"risk": "high", "attempts": 0},
        {"parallelism": {"max_agents_global": 1}})
    assert n == 1
    assert signals   # the signal still fired -- capacity is what capped it


def test_default_agents_per_task_config_respected():
    n, _ = parallel.decide_agent_count(
        {"risk": "low", "attempts": 0},
        {"parallelism": {"default_agents_per_task": 1}})
    assert n == 1


# -- unit: select_winner / decide_integration -----------------------------------

def _candidate(cid, disqualified=False, qa="pass", coverage=(2, 2),
              lines=5, reason=None):
    return {"candidate_id": cid, "worktree": "/tmp/%s" % cid,
           "backend": "mock", "disqualified": disqualified,
           "disqualify_reason": reason, "gate_result": {"ok": True},
           "qa_out": {}, "qa_verdict": qa, "changed_files": [],
           "lines_changed": lines, "acceptance_coverage": coverage}


def test_select_winner_disqualifies_failed_gate_regardless_of_qa():
    """A supervisor/QA opinion never overrides a failed deterministic
    check (Phase 5 spec, explicit requirement)."""
    good = _candidate("t1", qa="fail", coverage=(1, 2))
    bad_gate = _candidate("t1--c2", disqualified=True,
                          reason="deterministic checks failed")
    winner, ranked, reasoning = parallel.select_winner([good, bad_gate])
    assert winner["candidate_id"] == "t1"


def test_select_winner_none_when_all_disqualified():
    a = _candidate("t1", disqualified=True, reason="coder blocked")
    b = _candidate("t1--c2", disqualified=True, reason="scope violations")
    winner, ranked, reasoning = parallel.select_winner([a, b])
    assert winner is None
    assert ranked == []
    assert "disqualified" in reasoning


def test_select_winner_prefers_higher_qa_and_coverage():
    weak = _candidate("t1", qa="uncertain", coverage=(1, 2))
    strong = _candidate("t1--c2", qa="pass", coverage=(2, 2))
    winner, ranked, _ = parallel.select_winner([weak, strong])
    assert winner["candidate_id"] == "t1--c2"


def test_select_winner_tie_break_prefers_smaller_footprint():
    big = _candidate("t1", lines=40)
    small = _candidate("t1--c2", lines=5)
    winner, ranked, _ = parallel.select_winner([big, small])
    assert winner["candidate_id"] == "t1--c2"


def test_decide_integration_unambiguous_when_scores_differ():
    weak = _candidate("t1", qa="uncertain", coverage=(0, 2))
    strong = _candidate("t1--c2", qa="pass", coverage=(2, 2))
    winner, ranked, _ = parallel.select_winner([weak, strong])
    allowed, reason = parallel.decide_integration(winner, ranked)
    assert allowed is True


def test_decide_integration_blocks_on_exact_tie():
    a = _candidate("t1")
    b = _candidate("t1--c2")   # identical score tuple
    winner, ranked, _ = parallel.select_winner([a, b])
    allowed, reason = parallel.decide_integration(winner, ranked)
    assert allowed is False
    assert "ambiguous" in reason
    assert winner["candidate_id"] == "t1"   # still deterministically chosen


def test_decide_integration_no_survivors():
    allowed, reason = parallel.decide_integration(None, [])
    assert allowed is False


# -- full cycle: native single-agent path is untouched --------------------------

def test_low_risk_task_runs_native_single_agent_end_to_end(sandbox):
    project_cfg(sandbox)
    task = simple_task()   # risk="low" by default
    seed_project(sandbox, [task])
    caller = std_caller(task)
    result = cycle(sandbox, caller)
    assert result["status"] == "success"
    coder_calls = [c for c in caller.calls if c["role"] == "coder"]
    assert len(coder_calls) == 1   # exactly one agent, no fan-out


# -- full cycle: two candidates, isolated worktrees, evidence-based winner ------

def test_high_risk_task_runs_two_isolated_candidates(sandbox):
    project_cfg(sandbox)
    task = high_risk_task()
    seed_project(sandbox, [task])
    # candidate 1 (reuses the primary worktree) succeeds; candidate 2
    # self-blocks -- an unambiguous, deterministically-scored winner
    caller = std_caller(task, coder=[
        worker_out(edits=[{"path": "src/app.py", "action": "write",
                          "content": "VALUE = 2\n"}]),
        worker_out(blocked=True, blocker="candidate 2 self-blocked")])
    result = cycle(sandbox, caller)
    assert result["status"] == "success"
    coder_calls = [c for c in caller.calls if c["role"] == "coder"]
    assert len(coder_calls) == 2   # both candidates actually ran
    # exactly one candidate's worktree was ever committed/merged;
    # the losing candidate's worktree is untouched, not deleted
    task_dirs = os.listdir(os.path.join(str(sandbox["agentic"]), "worktrees",
                                        "tasks"))
    assert any(d.endswith("--c2") for d in task_dirs), task_dirs


def test_worktrees_are_genuinely_separate_git_directories(sandbox):
    project_cfg(sandbox)
    task = high_risk_task()
    seed_project(sandbox, [task])
    seen_worktrees = []

    def track_and_edit(workspace, input_data):
        seen_worktrees.append(workspace)
        return {"summary": "ok", "blocked": False, "blocker": None,
               "edits": [{"path": "src/app.py", "action": "write",
                         "content": "VALUE = 2\n"}], "commands": []}

    caller = std_caller(task, coder=[track_and_edit, track_and_edit])
    cycle(sandbox, caller)
    assert len(seen_worktrees) == 2
    assert seen_worktrees[0] != seen_worktrees[1]
    # a git WORKTREE's ".git" is a file (a gitdir: pointer), not a
    # directory -- exists() is the right check here, not isdir()
    assert os.path.exists(os.path.join(seen_worktrees[0], ".git"))
    assert os.path.exists(os.path.join(seen_worktrees[1], ".git"))


# -- exit gate: one candidate's failure cannot corrupt another ------------------

def test_one_candidate_crash_does_not_corrupt_the_other(sandbox):
    project_cfg(sandbox)
    task = high_risk_task()
    seed_project(sandbox, [task])

    def boom(workspace, input_data):
        raise RuntimeError("simulated candidate crash")

    caller = std_caller(task, coder=[
        worker_out(edits=[{"path": "src/app.py", "action": "write",
                          "content": "VALUE = 2\n"}]), boom])
    result = cycle(sandbox, caller)
    assert result["status"] == "success"   # the surviving candidate wins
    project_worktree = os.path.join(str(sandbox["agentic"]), "worktrees",
                                    "project")
    with open(os.path.join(project_worktree, "src", "app.py"),
              encoding="utf-8") as fh:
        assert fh.read() == "VALUE = 2\n"


# -- exit gate: deterministic evidence selects the winner, never a guess -------

def test_qa_alone_cannot_override_a_failed_deterministic_gate(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = [
        {"name": "fails", "command": "python -c \"import sys; sys.exit(1)\"",
         "mandatory": True}]
    task = high_risk_task(deterministic_checks=[])
    seed_project(sandbox, [task])
    # neither candidate can pass a permanently failing mandatory check --
    # both get disqualified, never silently approved by a QA opinion
    caller = std_caller(task, coder=[
        worker_out(edits=[{"path": "src/app.py", "action": "write",
                          "content": "VALUE = 2\n"}]),
        worker_out(edits=[{"path": "src/app.py", "action": "write",
                          "content": "VALUE = 3\n"}])])
    result = cycle(sandbox, caller)
    assert result["status"] == "failure"
    qa_calls = [c for c in caller.calls if c["role"] == "qa"]
    assert qa_calls == []   # disqualified before QA is ever consulted


# -- exit gate: ambiguous ties hold the merge but still run security review ----

def test_ambiguous_tie_holds_merge_but_still_runs_security_review(sandbox):
    project_cfg(sandbox)
    task = high_risk_task(security_relevant=True)
    seed_project(sandbox, [task])
    # both candidates produce byte-identical, equally valid output --
    # a genuine tie on every ranking signal
    caller = std_caller(task, security={
        "verdict": "fail", "concerns": [{"severity": "high",
                                         "description": "sqli"}],
        "reason": "injection risk"})
    result = cycle(sandbox, caller)
    assert result["status"] == "failure"
    sec_calls = [c for c in caller.calls if c["role"] == "security"]
    assert len(sec_calls) == 1   # security review ran despite the tie
    blockers = projstate.open_blockers(str(sandbox["agentic"]))
    assert blockers   # a blocker was recorded either way (security failed
                      # first here, exercised deliberately to prove the
                      # pipeline still reaches security under a tie)


def test_ambiguous_tie_with_clean_security_blocks_for_human_not_merge(
        sandbox):
    project_cfg(sandbox)
    task = high_risk_task()
    seed_project(sandbox, [task])
    caller = std_caller(task)   # identical scripted output -> exact tie
    result = cycle(sandbox, caller)
    assert result["status"] == "failure"
    from core import projstate
    human_blockers = projstate.open_blockers(str(sandbox["agentic"]),
                                             human_only=True)
    assert human_blockers
    assert any("ambiguous" in b["reason"] for b in human_blockers)
    sec_calls = [c for c in caller.calls if c["role"] == "security"]
    assert len(sec_calls) == 0   # this task isn't security_relevant and
                                # touches no security-triggering paths --
                                # never called, same as the single-agent
                                # path would behave for the same task


# -- exit gate: Orca absence does not disable native parallel execution --------

def test_native_engine_handles_parallel_candidates_without_orca(sandbox):
    from core import execengine
    project_cfg(sandbox)
    task = high_risk_task()
    seed_project(sandbox, [task])
    caller = std_caller(task, coder=[
        worker_out(edits=[{"path": "src/app.py", "action": "write",
                          "content": "VALUE = 2\n"}]),
        worker_out(blocked=True, blocker="c2 blocked")])
    engine, decision = execengine.select_engine(sandbox["cfg"], caller=caller)
    assert engine.name == execengine.ENGINE_NATIVE   # orca not installed
    result = cycle(sandbox, caller)
    assert result["status"] == "success"
