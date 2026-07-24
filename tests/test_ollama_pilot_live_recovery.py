"""Root-cause fix for the ollama-pilot t1-init-repo live blocker.

Evidence-based diagnosis (persisted run/memory evidence for the real
`ollama-pilot` project, inspected directly -- see the delivery report
for the full trail): the coder's structured JSON output was never the
problem -- every coder invocation that actually ran produced
schema-valid, successfully-parsed, successfully-applied edits. The
actual failure chain was:

1. A `.gitignore` written during the very first (pre-fix) attempt
   (run 20260722-181213) was left staged, uncommitted, in the task's
   preserved worktree.
2. `project.py`'s preserved-work compatibility check (added afterward)
   is designed to catch exactly this and revert the worktree -- but it
   only runs on an `action: execute` cycle, AFTER the conductor call.
3. Once the conductor started re-queuing the task instead of executing
   it (observed live: run 20260723-235604, with a fabricated "WORKER
   role" capability excuse that appears nowhere in this platform's
   actual prompts/code), that compatibility check was never reached, so
   the stale `.gitignore` was never cleaned -- a permanent stalemate.

This file locks in the two fixes: (a) `recover_expected_paths_contract_
bug` now reverts the preserved worktree itself, at recovery time, so
cleanup no longer depends on the conductor choosing to execute; (b) the
conductor prompt now explicitly documents directory-creation capability
and forbids inventing capability limitations or queuing over them."""
import os

from conftest import Clock, FakeCaller, git, project_cfg, proj_order, seed_project, simple_task, worker_out
from core import bootstrap_gate, config as config_mod, projstate, taskspace
from core.project import run_cycle


def qa_pass():
    return {"verdict": "pass", "done_when_results": [], "reason": "ok",
           "out_of_scope_changes": [], "test_integrity_preserved": True}


def sec_pass():
    return {"verdict": "pass", "concerns": [], "reason": "clean"}


def std_caller(task, coder=None, conductor=None, extra=None):
    scripted = {"conductor": conductor or proj_order(task),
               "coder": coder or worker_out(), "qa": qa_pass(),
               "security": sec_pass()}
    scripted.update(extra or {})
    return FakeCaller(scripted)


def _seed_stale_worktree(sandbox, task_id, stale_path="stale.gitignore",
                         stale_content="node_modules/\n"):
    """Reproduces the exact ollama-pilot shape: a task worktree that
    already exists (branch + files), with one file staged but never
    committed, left over from an earlier attempt whose allowed_paths
    later narrowed to exclude it."""
    root = str(sandbox["repo"])
    a = str(sandbox["agentic"])
    from core.project import PROJECT_BRANCH, ensure_project_worktree
    ensure_project_worktree(sandbox["cfg"], {
        "agentic": a, "root": root,
        "memory": str(sandbox["agentic"] / "memory"),
        "queue": str(sandbox["agentic"] / "queue"),
        "runs": str(sandbox["agentic"] / "runs")})
    path = taskspace.create_task_worktree(root, a, task_id, PROJECT_BRANCH)
    with open(os.path.join(path, stale_path), "w", encoding="utf-8") as fh:
        fh.write(stale_content)
    git(["add", "-A"], path)
    return path


# -- recovery now reverts the preserved worktree directly ----------------------

def test_recovery_reverts_preserved_worktree_even_without_a_later_cycle(
        sandbox):
    project_cfg(sandbox)
    task = simple_task("t1-init-repo", kind="bootstrap")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    path = _seed_stale_worktree(sandbox, "t1-init-repo")
    assert "stale.gitignore" in os.listdir(path)

    live_reason = ("Forbidden path .gitignore was touched during previous "
                   "cycle; WORKER role cannot edit paths outside "
                   "allowed_paths [package.json, index.html].")
    projstate.update_task(a, "t1-init-repo", status="blocked",
                          blocking_reason=live_reason)
    projstate.add_blocker(a, "t1-init-repo", live_reason, human_only=False)

    events = bootstrap_gate.recover_expected_paths_contract_bug(a)
    assert events == [{"task_id": "t1-init-repo", "resolved_blockers": 1,
                       "worktree_reverted": True}]
    # the stale file is gone -- reverted, not merely staged-over
    assert "stale.gitignore" not in os.listdir(path)


def test_recovery_worktree_revert_is_a_safe_no_op_when_nothing_preserved(
        sandbox):
    """No preserved worktree at all (never dispatched yet) -- recovery
    must not fail or fabricate a worktree."""
    project_cfg(sandbox)
    task = simple_task("t1-init-repo", kind="bootstrap")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    live_reason = "cannot edit paths outside allowed_paths"
    projstate.update_task(a, "t1-init-repo", status="blocked",
                          blocking_reason=live_reason)
    events = bootstrap_gate.recover_expected_paths_contract_bug(a)
    assert events == [{"task_id": "t1-init-repo", "resolved_blockers": 0,
                       "worktree_reverted": False}]


# -- end-to-end: cleanup no longer depends on the conductor executing ----------

def test_stale_worktree_stays_clean_even_if_conductor_requeues_again(
        sandbox):
    """The exact ollama-pilot stalemate, reproduced and locked shut: the
    conductor re-queuing on the very next cycle must no longer matter,
    because recovery already cleaned the worktree BEFORE the conductor
    was ever asked."""
    project_cfg(sandbox)
    task = simple_task("t1-init-repo", kind="bootstrap")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    path = _seed_stale_worktree(sandbox, "t1-init-repo")

    live_reason = ("Forbidden path .gitignore was touched during previous "
                   "cycle; WORKER role cannot edit paths outside "
                   "allowed_paths [package.json, index.html].")
    projstate.update_task(a, "t1-init-repo", status="blocked",
                          blocking_reason=live_reason)
    projstate.add_blocker(a, "t1-init-repo", live_reason, human_only=False)
    bootstrap_gate.recover_expected_paths_contract_bug(a)
    assert "stale.gitignore" not in os.listdir(path)

    # even a WORST-CASE conductor (queues again) can't resurrect staleness
    # -- recovery already cleaned it before this call ever happened
    caller = std_caller(task, conductor=proj_order(
        task, action="queue", queue_reason="still uncertain"))
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "failure"
    assert "stale.gitignore" not in os.listdir(path)


def test_full_cycle_succeeds_after_recovery_when_conductor_executes(
        sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = []
    task = simple_task("t1-init-repo", kind="bootstrap",
                       expected_paths=[{"path": "src", "type": "directory",
                                       "required": True},
                                      {"path": "package.json",
                                       "type": "file", "required": True,
                                       "non_empty": True}])
    test_setup = simple_task("t2-tests", kind="test_setup",
                             dependencies=["t1-init-repo"])
    seed_project(sandbox, [task, test_setup])
    a = str(sandbox["agentic"])
    path = _seed_stale_worktree(sandbox, "t1-init-repo")
    live_reason = ("Forbidden path .gitignore was touched during previous "
                   "cycle; WORKER role cannot edit paths outside "
                   "allowed_paths [package.json, index.html].")
    projstate.update_task(a, "t1-init-repo", status="blocked",
                          blocking_reason=live_reason)
    projstate.add_blocker(a, "t1-init-repo", live_reason, human_only=False)
    bootstrap_gate.recover_expected_paths_contract_bug(a)

    caller = std_caller(task, coder=worker_out(edits=[
        {"path": "package.json", "action": "write", "content": "{}\n"},
        {"path": "src", "action": "mkdir"}]),
        conductor=proj_order(task, allowed_paths=["package.json", "src/**"]))
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "success"
    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t1-init-repo"]["status"] == "done"


# -- conductor prompt no longer omits directory-creation capability ------------

def test_conductor_prompt_documents_directory_capability_and_forbids_fabrication():
    text = (config_mod.AGENTIC_DIR / "prompts"
           / "project-conductor.md").read_text(encoding="utf-8")
    low = text.lower()
    assert "mkdir" in low
    assert "git init" in low   # explicitly told never to ask for one
    assert "never invent" in low or "never write a work order" in low
    assert "queue" in low and "capabilit" in low
