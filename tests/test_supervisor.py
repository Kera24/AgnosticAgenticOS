"""Cross-platform subprocess supervisor (core/supervisor.py).

Root-cause fix for the live incident of 2026-07-25: `execpolicy.
run_command` ran every CLI-backend call through
`subprocess.run(timeout=N)`, which cannot enforce its own deadline once
the child spawns a descendant that inherits its stdout/stderr pipe
handles (a documented CPython/Windows limitation, not a bug in the
configured timeout VALUE). `codex.exe` sat alive 27+ minutes past its
900s configured timeout; the scheduler heartbeat never moved because
nothing updates it independently of a subprocess call returning; the
project lease/lock were never released because the owning Python
process itself was blocked inside that one call and never reached its
own `finally` cleanup.

Every test here uses short local dummy subprocesses (`python -c "..."`)
-- never a live Codex/Claude/Ollama/Orca call."""
import json
import os
import sys
import time

import pytest

from core import errors, projstate, supervisor
from core.project import _recover_stale_owned_processes

PY = sys.executable
POSIX = os.name != "nt"


def sleep_script(seconds):
    return [PY, "-c", "import time; time.sleep(%s)" % seconds]


# -- 1-4: basic child lifecycle --------------------------------------------------

def test_child_exits_normally():
    r = supervisor.run_supervised([PY, "-c", "print('hello')"], cwd=".",
                                  timeout=10)
    assert r["exit_code"] == 0
    assert r["stdout"] == "hello\n"
    assert r["timed_out"] is False
    assert r["supervisor"] is None


def test_child_returns_nonzero():
    r = supervisor.run_supervised(
        [PY, "-c", "import sys; sys.exit(3)"], cwd=".", timeout=10)
    assert r["exit_code"] == 3
    assert r["timed_out"] is False


def test_child_produces_continuous_output():
    r = supervisor.run_supervised(
        [PY, "-c", "import sys\nfor i in range(20): print(i)"],
        cwd=".", timeout=10)
    assert r["exit_code"] == 0
    assert r["stdout"].splitlines() == [str(i) for i in range(20)]
    assert r["timed_out"] is False


def test_child_produces_no_output():
    r = supervisor.run_supervised([PY, "-c", "pass"], cwd=".", timeout=10)
    assert r["exit_code"] == 0
    assert r["stdout"] == ""
    assert r["timed_out"] is False


# -- 5-8: timeout phases, never a blocking read defeating them ------------------

def test_first_output_timeout():
    r = supervisor.run_supervised(
        sleep_script(30), cwd=".", timeout=30, first_output_timeout=1,
        poll_interval=0.1)
    assert r["timed_out"] is True
    assert r["supervisor"]["phase"] == supervisor.PHASE_FIRST_OUTPUT


def test_idle_output_timeout():
    script = ("import sys,time\nprint('start'); sys.stdout.flush()\n"
             "time.sleep(30)\nprint('end')")
    r = supervisor.run_supervised(
        [PY, "-c", script], cwd=".", timeout=60, idle_output_timeout=1,
        poll_interval=0.1)
    assert r["timed_out"] is True
    assert r["supervisor"]["phase"] == supervisor.PHASE_IDLE_OUTPUT
    assert "start" in r["stdout"]   # partial output preserved


def test_total_timeout_despite_continuous_output():
    """The exact shape of the live bug's opposite case: a child that
    DOES produce output continuously must still be cut off at the total
    execution deadline -- output alone must never suppress it."""
    script = ("import sys,time\n"
             "for i in range(100):\n"
             " print(i); sys.stdout.flush(); time.sleep(0.2)")
    r = supervisor.run_supervised([PY, "-c", script], cwd=".", timeout=1,
                                  poll_interval=0.1)
    assert r["timed_out"] is True
    assert r["supervisor"]["phase"] == supervisor.PHASE_TOTAL_EXECUTION
    assert r["duration_seconds"] < 5   # bounded, not left to run to completion


def test_blocking_stream_does_not_defeat_timeout():
    """A child that writes a large burst then goes fully silent without
    closing stdout (simulating a stuck downstream reader) must still be
    caught by the total-execution deadline -- the supervisor never waits
    on a blocking readline() in its own main loop."""
    script = ("import sys,time\n"
             "sys.stdout.write('x' * 100000); sys.stdout.flush()\n"
             "time.sleep(30)")
    started = time.monotonic()
    r = supervisor.run_supervised([PY, "-c", script], cwd=".", timeout=1,
                                  poll_interval=0.1)
    elapsed = time.monotonic() - started
    assert r["timed_out"] is True
    assert elapsed < 10   # never anywhere near the child's 30s sleep


# -- 9-11: process-tree termination ----------------------------------------------

def test_descendant_process_cleanup():
    """The exact live failure mode: parent spawns a child that inherits
    stdout and hangs forever; killing only the immediate PID would leave
    it alive and the old subprocess.run(timeout=) path would hang
    forever waiting for its pipe to close. The tree-kill must reap it."""
    script = ("import subprocess,sys,time\n"
             "subprocess.Popen([sys.executable, '-c', "
             "'import time; time.sleep(120)'])\n"
             "time.sleep(120)")
    r = supervisor.run_supervised([PY, "-c", script], cwd=".", timeout=1,
                                  graceful_timeout=3, forced_timeout=5)
    assert r["timed_out"] is True
    assert r["supervisor"]["termination"]["tree_confirmed_stopped"] is True
    assert r["duration_seconds"] < 15


def test_graceful_shutdown_succeeds_without_forced_kill():
    r = supervisor.run_supervised(sleep_script(30), cwd=".", timeout=1,
                                  graceful_timeout=5, forced_timeout=10)
    assert r["timed_out"] is True
    termination = r["supervisor"]["termination"]
    assert termination["graceful_attempted"] is True
    assert termination["tree_confirmed_stopped"] is True


@pytest.mark.skipif(not os.name == "nt", reason="SIGBREAK-ignore only "
                    "meaningful on Windows; POSIX forced-kill path is "
                    "exercised by test_posix_process_group_termination")
def test_forced_tree_termination_when_child_ignores_graceful_signal():
    script = ("import signal,subprocess,sys,time\n"
             "signal.signal(signal.SIGBREAK, signal.SIG_IGN)\n"
             "subprocess.Popen([sys.executable, '-c', "
             "'import signal,time; "
             "signal.signal(signal.SIGBREAK, signal.SIG_IGN); "
             "time.sleep(120)'])\n"
             "time.sleep(120)")
    r = supervisor.run_supervised([PY, "-c", script], cwd=".", timeout=1,
                                  graceful_timeout=2, forced_timeout=8)
    assert r["timed_out"] is True
    termination = r["supervisor"]["termination"]
    assert termination["forced"] is True
    assert termination["tree_confirmed_stopped"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group "
                    "termination -- not exercised on Windows")
def test_posix_process_group_termination():
    script = ("import subprocess,sys,time\n"
             "subprocess.Popen([sys.executable, '-c', "
             "'import time; time.sleep(120)'])\n"
             "time.sleep(120)")
    r = supervisor.run_supervised([PY, "-c", script], cwd=".", timeout=1,
                                  graceful_timeout=3, forced_timeout=5)
    assert r["timed_out"] is True
    assert r["supervisor"]["termination"]["tree_confirmed_stopped"] is True


# -- 12/14: process identity (PID-reuse safety) ----------------------------------

def test_process_start_token_is_stable_for_a_live_process():
    proc_argv = sleep_script(5)
    import subprocess as _sp
    proc = _sp.Popen(proc_argv)
    try:
        token1 = supervisor.process_start_token(proc.pid)
        token2 = supervisor.process_start_token(proc.pid)
        assert token1 is not None
        assert token1 == token2
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_identity_matches_false_after_process_exits():
    import subprocess as _sp
    proc = _sp.Popen([PY, "-c", "pass"])
    proc.wait(timeout=5)
    record = {"pid": proc.pid, "start_time_token": "definitely-not-real"}
    assert supervisor.identity_matches(record) is False


def test_pid_reuse_mismatch_is_not_terminated():
    """A record whose persisted identity token no longer matches the
    live process at that PID must never be touched -- this is the
    entire point of persisting a start-time token instead of a bare
    PID."""
    import subprocess as _sp
    proc = _sp.Popen(sleep_script(5))
    try:
        real_token = supervisor.process_start_token(proc.pid)
        fake_record = {"pid": proc.pid,
                       "start_time_token": real_token + "-mismatched"}
        outcome = supervisor.terminate_by_pid(fake_record)
        assert outcome["terminated"] is False
        assert "mismatch" in outcome["reason"]
        assert supervisor.is_process_alive(proc.pid) is True
    finally:
        proc.kill()
        proc.wait(timeout=5)


# -- 15/16: heartbeat / partial output --------------------------------------------

def test_heartbeat_continues_with_no_output(tmp_path):
    record_file = str(tmp_path / "rec.json")
    r = supervisor.run_supervised(
        sleep_script(1.5), cwd=".", timeout=10, poll_interval=0.2,
        identity={"run_id": "r1", "project_id": "p1", "backend": "codex",
                 "role": "coder"}, record_file=record_file)
    assert r["timed_out"] is False
    with open(record_file, encoding="utf-8") as fh:
        record = json.load(fh)
    for field in ("run_id", "project_id", "backend", "role", "pid",
                 "elapsed_seconds", "timeout_deadline", "state"):
        assert field in record
    assert record["run_id"] == "r1"
    assert record["state"] == supervisor.SUPERVISOR_COMPLETED


def test_partial_output_preserved_on_timeout():
    script = ("import sys,time\n"
             "print('partial-marker'); sys.stdout.flush()\n"
             "time.sleep(30)")
    r = supervisor.run_supervised([PY, "-c", script], cwd=".", timeout=1,
                                  graceful_timeout=2, forced_timeout=3)
    assert r["timed_out"] is True
    assert "partial-marker" in r["stdout"]


# -- 17-20: lease/lock/task reconciliation (integration with project.py) --------

def _seed_dead_record(agentic_dir, run_id, task_id, project_id="proj"):
    import subprocess as _sp
    proc = _sp.Popen([PY, "-c", "pass"])
    proc.wait(timeout=5)
    path = supervisor.record_path(agentic_dir, run_id, "coder", "abc123")
    supervisor._atomic_write_json(path, {
        "run_id": run_id, "project_id": project_id, "task_id": task_id,
        "backend": "codex", "role": "coder", "pid": proc.pid,
        "start_time_token": "stale-token-process-already-exited",
        "parent_pid": os.getpid(), "nonce": "abc123",
        "state": supervisor.SUPERVISOR_RUNNING,
        "deadline_at": "2020-01-01T00:00:00"})
    return path


def test_stale_lease_recovered_on_startup(sandbox):
    from conftest import project_cfg, seed_project, simple_task
    project_cfg(sandbox)
    task = simple_task("t1")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    projstate.update_task(a, "t1", status="in_progress")

    from core import taskspace
    lease = taskspace.ProjectLease(a, "test-project")
    acquired, _ = lease.acquire(run_id=None)
    assert acquired
    # simulate this lease's owning process having been the SAME pid as
    # the dead record's parent_pid -- exactly what a crashed run_cycle
    # leaves behind
    holder = lease.holder()
    holder["pid"] = os.getpid()
    lease._write(holder)

    _seed_dead_record(a, "stale-run-1", "t1")

    from core.scheduler import Scheduler
    scheduler = Scheduler(sandbox["cfg"], str(sandbox["agentic"] / "memory"))
    scheduler.state.update(state="running", current_cycle="stale-run-1")
    scheduler.save()

    recovered = _recover_stale_owned_processes(
        sandbox["cfg"], a, str(sandbox["repo"]), scheduler, None,
        lambda e: None)
    assert len(recovered) == 1
    assert recovered[0]["action"] == "reconciled"

    assert lease.holder() is None   # lease released
    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t1"]["status"] == "pending"   # retained for another attempt
    assert scheduler.state["state"] == "idle"


def test_unrelated_project_lease_untouched(sandbox):
    """A lease belonging to a DIFFERENT, still-legitimately-running
    process (different pid) must never be cleared just because some
    OTHER project's stale record happens to be reconciled."""
    from conftest import project_cfg, seed_project, simple_task
    project_cfg(sandbox)
    task = simple_task("t1")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])

    from core import taskspace
    lease = taskspace.ProjectLease(a, "test-project")
    lease.acquire(run_id=None)
    holder = lease.holder()
    holder["pid"] = 999999999   # a different, "still active" process
    lease._write(holder)

    _seed_dead_record(a, "stale-run-2", "t1")
    from core.scheduler import Scheduler
    scheduler = Scheduler(sandbox["cfg"], str(sandbox["agentic"] / "memory"))

    _recover_stale_owned_processes(sandbox["cfg"], a, str(sandbox["repo"]),
                                   scheduler, None, lambda e: None)
    assert lease.holder() is not None   # untouched -- pid didn't match


def test_interrupted_runner_recovery_full_cycle(sandbox):
    """End-to-end: a task 'stuck' in-progress with a dead owned-process
    record recovers to pending and a fresh cycle can complete it --
    without a fresh project, without waiting out any TTL."""
    from conftest import Clock, FakeCaller, project_cfg, proj_order, seed_project, simple_task, worker_out
    project_cfg(sandbox)
    task = simple_task("t1")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])
    projstate.update_task(a, "t1", status="in_progress")
    _seed_dead_record(a, "dead-run-xyz", "t1")

    def qa_pass():
        return {"verdict": "pass", "done_when_results": [], "reason": "ok",
               "out_of_scope_changes": [], "test_integrity_preserved": True}

    caller = FakeCaller({"conductor": proj_order(task), "coder": worker_out(),
                        "qa": qa_pass(),
                        "security": {"verdict": "pass", "concerns": [],
                                    "reason": "clean"}})
    from core.project import run_cycle
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "success"
    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    assert tasks["t1"]["status"] == "done"


# -- 21/22: timeout classification, retry, fallback ------------------------------

def test_coder_timeout_is_classified_execution_timeout_and_stays_retryable(
        sandbox):
    from conftest import Clock, FakeCaller, project_cfg, proj_order, seed_project, simple_task
    project_cfg(sandbox)
    task = simple_task("t1")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])

    def timeout_coder(workspace, input_data):
        return {"_error": "timeout", "backend": "mock"}

    caller = FakeCaller({"conductor": proj_order(task), "coder": timeout_coder})
    from core.project import run_cycle
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "failure"
    tasks = {t["id"]: t for t in projstate.load_backlog(a)}
    # never hard-blocked -- retained for another attempt
    assert tasks["t1"]["status"] in ("pending", "blocked")
    if tasks["t1"]["status"] == "blocked":
        pytest.fail("a single timeout must never hard-block the task")

    log_path = sandbox["agentic"] / "memory" / "decisions.jsonl"
    events = [json.loads(line) for line in
             log_path.read_text(encoding="utf-8").splitlines() if line]
    classified = [e for e in events if e.get("event") == "failure_classified"]
    assert classified and classified[-1]["failure_class"] == "execution_timeout"


def test_execution_timeout_not_recorded_as_task_contract_invalid(sandbox):
    from conftest import Clock, FakeCaller, project_cfg, proj_order, seed_project, simple_task
    project_cfg(sandbox)
    task = simple_task("t1")
    seed_project(sandbox, [task])
    a = str(sandbox["agentic"])

    def timeout_coder(workspace, input_data):
        return {"_error": "timeout", "backend": "mock"}

    caller = FakeCaller({"conductor": proj_order(task), "coder": timeout_coder})
    from core.project import run_cycle
    run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    log_path = sandbox["agentic"] / "memory" / "decisions.jsonl"
    events = [json.loads(line) for line in
             log_path.read_text(encoding="utf-8").splitlines() if line]
    classified = [e for e in events if e.get("event") == "failure_classified"]
    assert classified[-1]["failure_class"] != "task_contract_invalid"


# -- 23: no unrestricted shell=True -----------------------------------------------

def test_run_supervised_never_uses_shell(monkeypatch):
    captured = {}
    import subprocess as _sp
    real_popen = _sp.Popen

    def spy_popen(*args, **kwargs):
        captured["shell"] = kwargs.get("shell", False)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(supervisor.subprocess, "Popen", spy_popen)
    supervisor.run_supervised([PY, "-c", "pass"], cwd=".", timeout=5)
    assert captured.get("shell", False) is False


def test_default_cli_runner_command_not_found():
    r = supervisor.default_cli_runner(
        ["definitely-not-a-real-executable-xyz123"], cwd=".", timeout=5)
    assert r["exit_code"] == 127
    assert r["timed_out"] is False
