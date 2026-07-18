"""MP Phase 3 — per-task worktrees, ownership claims, leases, integration."""
import datetime
import json
import os

import pytest

from conftest import (Clock, FakeCaller, project_cfg, proj_order,
                      seed_project, simple_task, verifier_out, worker_out)
from core import errors, gitops, projstate, taskspace
from core.project import run_cycle
from core.taskspace import (ProjectLease, active_claims, claim_paths,
                            cleanup_task_worktree, create_task_worktree,
                            integrate_task, recover_abandoned,
                            release_claim)


def qa_pass():
    return verifier_out("pass")


def sec_pass():
    return {"verdict": "pass", "concerns": [], "reason": "clean"}


def std_caller(task, **over):
    scripted = {"conductor": proj_order(task), "coder": worker_out(),
                "qa": qa_pass(), "security": sec_pass()}
    scripted.update(over)
    return FakeCaller(scripted)


# -- ownership claims -------------------------------------------------------------

def test_claim_overlap_refused(tmp_path):
    a = str(tmp_path)
    claim_paths(a, "t1", ["src/app.py", "src/util/**"])
    with pytest.raises(errors.PolicyError, match="ownership overlap"):
        claim_paths(a, "t2", ["src/app.py"])
    with pytest.raises(errors.PolicyError, match="ownership overlap"):
        claim_paths(a, "t3", ["src/util/helpers.py"])
    claim_paths(a, "t4", ["docs/**"])              # disjoint is fine
    release_claim(a, "t1")
    claim_paths(a, "t5", ["src/app.py"])           # released -> claimable
    assert set(active_claims(a)) == {"t4", "t5"}


def test_exclusive_classes_conflict(tmp_path):
    a = str(tmp_path)
    claim_paths(a, "t1", ["supabase/migrations/0001_init.sql"])
    with pytest.raises(errors.PolicyError, match="migrations"):
        claim_paths(a, "t2", ["db/migrations/0002_more.sql"])
    claim_paths(a, "t3", ["src/app.py"])   # non-exclusive path still fine


def test_dependency_class_exclusive(tmp_path):
    a = str(tmp_path)
    claim_paths(a, "t1", ["package.json"])
    with pytest.raises(errors.PolicyError, match="dependencies"):
        claim_paths(a, "t2", ["requirements.txt"])


# -- worktrees ----------------------------------------------------------------------

def test_task_worktree_lifecycle(sandbox):
    repo = str(sandbox["repo"])
    a = str(sandbox["agentic"])
    # integration target
    gitops.run_git(["worktree", "add", "-b", "agentic/project",
                    os.path.join(a, "worktrees", "project"), "HEAD"],
                   cwd=repo)
    project_wt = os.path.join(a, "worktrees", "project")
    task_wt = create_task_worktree(repo, a, "t1-first", "agentic/project")
    assert os.path.isdir(os.path.join(task_wt, ".git")
                         if os.path.isdir(os.path.join(task_wt, ".git"))
                         else task_wt)
    with open(os.path.join(task_wt, "src", "app.py"), "w") as fh:
        fh.write("VALUE = 2\n")
    gitops.commit_all(task_wt, "task work")
    integrate_task(repo, project_wt, task_wt, "t1-first", "integrate t1")
    merged = open(os.path.join(project_wt, "src", "app.py")).read()
    assert merged == "VALUE = 2\n"
    log = gitops.run_git(["log", "--oneline", "-2"], cwd=project_wt)
    assert "integrate t1" in log
    result = cleanup_task_worktree(repo, a, "t1-first", success=True)
    assert "removed" in result
    assert not os.path.exists(task_wt)
    branches = gitops.run_git(["branch", "--list", "agentic/task/t1-first"],
                              cwd=repo, check=False)
    assert not branches.strip()


def test_dirty_integration_target_refused(sandbox):
    repo = str(sandbox["repo"])
    a = str(sandbox["agentic"])
    project_wt = os.path.join(a, "worktrees", "project")
    gitops.run_git(["worktree", "add", "-b", "agentic/project", project_wt,
                    "HEAD"], cwd=repo)
    task_wt = create_task_worktree(repo, a, "t1", "agentic/project")
    with open(os.path.join(task_wt, "src", "app.py"), "w") as fh:
        fh.write("VALUE = 2\n")
    gitops.commit_all(task_wt, "work")
    with open(os.path.join(project_wt, "dirty.txt"), "w") as fh:
        fh.write("uncommitted\n")
    with pytest.raises(errors.PolicyError, match="dirty"):
        integrate_task(repo, project_wt, task_wt, "t1", "msg")


def test_failed_worktree_preserved_as_evidence(sandbox):
    repo = str(sandbox["repo"])
    a = str(sandbox["agentic"])
    gitops.run_git(["worktree", "add", "-b", "agentic/project",
                    os.path.join(a, "worktrees", "project"), "HEAD"],
                   cwd=repo)
    task_wt = create_task_worktree(repo, a, "t9", "agentic/project")
    result = cleanup_task_worktree(repo, a, "t9", success=False)
    assert "kept" in result
    assert os.path.exists(task_wt)


def test_recover_abandoned_after_restart(sandbox):
    repo = str(sandbox["repo"])
    a = str(sandbox["agentic"])
    gitops.run_git(["worktree", "add", "-b", "agentic/project",
                    os.path.join(a, "worktrees", "project"), "HEAD"],
                   cwd=repo)
    task_wt = create_task_worktree(repo, a, "ghost", "agentic/project")
    old = datetime.datetime.now().timestamp() - 100000
    os.utime(task_wt, (old, old))
    abandoned = recover_abandoned(repo, a)
    assert [entry["task_id"] for entry in abandoned] == ["ghost"]
    # an actively claimed worktree is NOT abandoned
    claim_paths(a, "ghost", ["src/**"])
    assert recover_abandoned(repo, a) == []


# -- leases --------------------------------------------------------------------------

def test_lease_acquire_conflict_expiry_release(tmp_path):
    clock = Clock()
    a = str(tmp_path)
    mine = ProjectLease(a, "p1", ttl_seconds=600, clock=clock)
    ok, lease = mine.acquire(run_id="r1")
    assert ok and lease["status"] == "active"
    # simulate another process on another machine holding the lease
    foreign = dict(lease, machine_id="other-box", pid=99999)
    with open(mine.path, "w", encoding="utf-8") as fh:
        json.dump(foreign, fh)
    ok2, holder = mine.acquire()
    assert not ok2 and holder["machine_id"] == "other-box"
    # expiry frees it
    clock.advance(minutes=11)
    ok3, _ = mine.acquire(run_id="r2")
    assert ok3
    assert mine.renew()
    assert mine.release()
    assert mine.holder() is None


# -- run_cycle integration ------------------------------------------------------------

def test_cycle_uses_task_worktree_and_integrates(sandbox):
    cfg = project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    result = run_cycle(cfg, caller=std_caller(task), clock=Clock())
    assert result["status"] == "success"
    a = str(sandbox["agentic"])
    project_wt = os.path.join(a, "worktrees", "project")
    # work landed on agentic/project via a local merge
    assert open(os.path.join(project_wt, "src", "app.py")).read() \
        == "VALUE = 2\n"
    log = gitops.run_git(["log", "--oneline", "-3"], cwd=project_wt)
    assert "t1-first" in log
    # successful task worktree cleaned up; claim released
    assert not os.path.exists(os.path.join(a, "worktrees", "tasks",
                                           "t1-first"))
    assert active_claims(a) == {}
    # never pushed anywhere
    remotes = gitops.run_git(["remote"], cwd=str(sandbox["repo"]),
                             check=False)
    assert remotes.strip() == ""


def test_preclaimed_paths_block_cycle(sandbox):
    cfg = project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    claim_paths(str(sandbox["agentic"]), "other-task", ["src/**"])
    result = run_cycle(cfg, caller=std_caller(task), clock=Clock())
    assert result["status"] == "failure"
    assert "ownership" in result["detail"]
    tasks = projstate.load_backlog(str(sandbox["agentic"]))
    assert tasks[0]["status"] == "blocked"


def test_foreign_lease_blocks_cycle(sandbox):
    cfg = project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    clock = Clock()
    lease = ProjectLease(str(sandbox["agentic"]), "test", clock=clock)
    ok, mine = lease.acquire()
    foreign = dict(mine, machine_id="colleague-pc", pid=4242)
    with open(lease.path, "w", encoding="utf-8") as fh:
        json.dump(foreign, fh)
    result = run_cycle(cfg, caller=std_caller(task), clock=clock)
    assert result["status"] == "lease_held"
    assert result["holder"]["machine_id"] == "colleague-pc"
    # expired lease no longer blocks
    clock.advance(minutes=90)
    result2 = run_cycle(cfg, caller=std_caller(task), clock=clock)
    assert result2["status"] == "success"


def test_failed_cycle_keeps_task_worktree_evidence(sandbox):
    cfg = project_cfg(sandbox)
    sandbox["cfg"]["verification"]["commands"] = [
        {"name": "always-fails",
         "command": ["python", "-c", "import sys; sys.exit(1)"],
         "mandatory": True}]
    from core import gate
    gate.save_baseline(str(sandbox["agentic"]),
                       [{"name": "always-fails", "passed": True}])
    task = simple_task()
    seed_project(sandbox, [task])
    result = run_cycle(cfg, caller=std_caller(task), clock=Clock())
    assert result["status"] == "failure"
    a = str(sandbox["agentic"])
    assert os.path.exists(os.path.join(a, "worktrees", "tasks", "t1-first"))
    assert active_claims(a) == {}          # claim released, evidence kept
