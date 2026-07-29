"""Cross-platform subprocess supervisor for CLI backends (Codex, Claude
Code, Qwen, Orca, and any future CLI backend).

Root cause this module fixes (see docs/phase1-5-final-report.md and the
live ollama-pilot incident of 2026-07-25): `execpolicy.run_command` ran
every CLI invocation through `subprocess.run(..., timeout=N)`, which
CPython implements as `Popen.communicate(timeout=N)`. On a timeout,
`subprocess.run` kills only the immediate child PID and then -- on
Windows specifically -- calls `Popen.communicate()` a SECOND time with
NO timeout at all (see cpython's subprocess.py, the `_mswindows`
branch of `run()`) to drain any buffered output. If that child spawned
descendants that inherited the stdout/stderr pipe handles (routine for
sandboxed CLI tools), killing only the parent leaves those handles
open, and the second, untimed `communicate()` call blocks forever
waiting for EOF that never comes. This is exactly what was observed
live: `codex.exe` (and whatever it spawned) sat alive for 27+ minutes,
`execution.command_timeout_seconds: 900` never fired, the scheduler
heartbeat froze at the moment the cycle began (never updated again,
because nothing in the codebase updates it independently of a
subprocess call returning), and the project lease/lock were never
released because the owning Python process itself was blocked inside
that single call and never reached its own `finally` cleanup.

Fix: never call `subprocess.run`/`Popen.communicate(timeout=...)` for a
CLI backend invocation. Spawn with `Popen` directly, own the process
group (Windows: CREATE_NEW_PROCESS_GROUP; POSIX: start_new_session),
drain stdout/stderr in daemon reader threads that never block the main
supervision loop, and evaluate every deadline (first output, idle
output, total execution) against `time.monotonic()` on our own timer,
independent of whether the child has produced any output at all. A
timeout always terminates the OWNED PROCESS TREE (not just the
immediate PID) and always returns control to the caller within a
bounded, predictable time -- graceful_shutdown + forced_termination at
worst, never indefinitely.
"""
import datetime as _dt
import json
import os
import signal
import subprocess
import threading
import time
import uuid

from . import execpolicy

_local = threading.local()

SUPERVISOR_STARTING = "starting"
SUPERVISOR_RUNNING = "running"
SUPERVISOR_GRACEFUL_SHUTDOWN = "graceful_shutdown"
SUPERVISOR_FORCE_TERMINATING = "force_terminating"
SUPERVISOR_COMPLETED = "completed"
SUPERVISOR_TIMEOUT = "timeout"

PHASE_FIRST_OUTPUT = "first_output"
PHASE_IDLE_OUTPUT = "idle_output"
PHASE_TOTAL_EXECUTION = "total_execution"

DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_GRACEFUL_TIMEOUT = 10.0
DEFAULT_FORCED_TIMEOUT = 15.0

_IS_WINDOWS = (os.name == "nt")


def _now_iso():
    return _dt.datetime.now().isoformat(timespec="seconds")


# -- process identity (PID-reuse-safe) ------------------------------------------

def _win_process_start_token(pid):
    """An opaque, comparable identity token (the raw FILETIME creation
    timestamp) for a Windows PID, or None if the process doesn't exist
    or can't be queried. Two different processes that happen to reuse
    the same PID will (for all practical purposes) never share this
    token -- FILETIME has 100ns resolution."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                  int(pid))
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_t = wintypes.FILETIME()
        kernel_t = wintypes.FILETIME()
        user_t = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_t),
            ctypes.byref(kernel_t), ctypes.byref(user_t))
        if not ok:
            return None
        value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return str(value)
    finally:
        kernel32.CloseHandle(handle)


def _posix_process_start_token(pid):
    """`ps -o lstart=` for the pid, or None if it doesn't exist. Portable
    across Linux/macOS without a /proc dependency."""
    try:
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=5)
    except Exception:   # noqa: BLE001 -- identity probing is best-effort
        return None
    text = (result.stdout or "").strip()
    return text or None


def process_start_token(pid):
    if _IS_WINDOWS:
        return _win_process_start_token(pid)
    return _posix_process_start_token(pid)


def is_process_alive(pid):
    """Liveness only, no identity check -- used where a false positive
    (treating a reused PID as 'still the same process') would be
    unsafe, and where callers must instead compare `process_start_token`
    explicitly before acting."""
    if _IS_WINDOWS:
        return process_start_token(pid) is not None
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, just not ours
    except OSError:
        return False


def identity_matches(record):
    """True only if the process recorded in `record` is still alive AND
    is verifiably the SAME process (start-time token matches) -- never
    terminate or reconcile based on PID number alone."""
    pid = record.get("pid")
    token = record.get("start_time_token")
    if not pid or not token:
        return False
    current = process_start_token(pid)
    return current is not None and current == token


# -- process ownership records (persisted, survive process restarts) -----------

def records_dir(agentic_dir):
    return os.path.join(str(agentic_dir), "processes")


def record_path(agentic_dir, run_id, role, nonce):
    return os.path.join(records_dir(agentic_dir),
                        "%s__%s__%s.json" % (run_id, role, nonce))


def _atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def load_record(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def list_records(agentic_dir):
    directory = records_dir(agentic_dir)
    if not os.path.isdir(directory):
        return []
    out = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        record = load_record(os.path.join(directory, name))
        if record is not None:
            record["_path"] = os.path.join(directory, name)
            out.append(record)
    return out


def find_running_record(agentic_dir, run_id):
    for record in list_records(agentic_dir):
        if record.get("run_id") == run_id and \
                record.get("state") in (SUPERVISOR_STARTING,
                                        SUPERVISOR_RUNNING,
                                        SUPERVISOR_GRACEFUL_SHUTDOWN,
                                        SUPERVISOR_FORCE_TERMINATING):
            return record
    return None


# -- process-tree termination ---------------------------------------------------

def _send_graceful_signal(proc):
    try:
        if _IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        return True
    except Exception:   # noqa: BLE001 -- best-effort; forced path follows
        return False


def _force_kill_tree_by_pid(pid):
    """Kill the OWNED process tree rooted at `pid`. Windows: `taskkill
    /PID <pid> /T /F` -- targeted by PID, never by executable name, so
    an unrelated codex/claude/ollama session elsewhere on the machine
    is never touched. POSIX: SIGKILL to the whole owned process
    group."""
    if _IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=20,
                creationflags=execpolicy.windows_background_creationflags())
        except Exception:   # noqa: BLE001
            pass
        return
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
    except Exception:   # noqa: BLE001
        pass


def terminate_tree(proc, graceful_timeout=DEFAULT_GRACEFUL_TIMEOUT,
                   forced_timeout=DEFAULT_FORCED_TIMEOUT,
                   on_state=None):
    """Terminate the process this supervisor owns: graceful signal to
    the whole group first, forced tree-kill after the grace period,
    then verify. Always returns within
    graceful_timeout + forced_timeout at the very most -- never blocks
    indefinitely, regardless of whether the child (or its descendants)
    cooperate."""
    if on_state:
        on_state(SUPERVISOR_GRACEFUL_SHUTDOWN)
    graceful_attempted = _send_graceful_signal(proc)
    deadline = time.monotonic() + graceful_timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return {"graceful_attempted": graceful_attempted, "forced": False,
                    "tree_confirmed_stopped": True}
        time.sleep(0.2)

    if on_state:
        on_state(SUPERVISOR_FORCE_TERMINATING)
    _force_kill_tree_by_pid(proc.pid)
    deadline = time.monotonic() + forced_timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    return {"graceful_attempted": graceful_attempted, "forced": True,
           "tree_confirmed_stopped": proc.poll() is not None}


def terminate_by_pid(record, graceful_timeout=DEFAULT_GRACEFUL_TIMEOUT,
                     forced_timeout=DEFAULT_FORCED_TIMEOUT):
    """Same termination sequence, but for a PID we don't hold a live
    `Popen` handle for (operator commands, startup recovery). Verifies
    identity against `record` first and refuses to act on a mismatch --
    a PID belonging to a different, newer process is never touched."""
    if not identity_matches(record):
        return {"terminated": False,
               "reason": "process identity mismatch or already gone -- "
                         "refusing to act on a possibly-reused PID"}
    pid = record["pid"]
    graceful_attempted = False
    try:
        if _IS_WINDOWS:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        graceful_attempted = True
    except Exception:   # noqa: BLE001
        pass
    deadline = time.monotonic() + graceful_timeout
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            return {"terminated": True,
                   "termination": {"graceful_attempted": graceful_attempted,
                                   "forced": False,
                                   "tree_confirmed_stopped": True}}
        time.sleep(0.2)
    _force_kill_tree_by_pid(pid)
    deadline = time.monotonic() + forced_timeout
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            break
        time.sleep(0.2)
    stopped = not is_process_alive(pid)
    return {"terminated": stopped,
           "termination": {"graceful_attempted": graceful_attempted,
                           "forced": True, "tree_confirmed_stopped": stopped}}


# -- the supervised run ----------------------------------------------------------

class TimeoutConfig:
    __slots__ = ("total_execution", "first_output", "idle_output",
                "graceful_shutdown", "forced_termination", "poll_interval")

    def __init__(self, total_execution, first_output=None, idle_output=None,
                graceful_shutdown=DEFAULT_GRACEFUL_TIMEOUT,
                forced_termination=DEFAULT_FORCED_TIMEOUT,
                poll_interval=DEFAULT_POLL_INTERVAL):
        self.total_execution = float(total_execution)
        # unset first_output/idle_output default to "no narrower than
        # total_execution" -- i.e. a pure no-op unless explicitly
        # configured tighter, so existing single-timeout callers see no
        # behavioural change beyond "the timeout now actually fires".
        self.first_output = float(first_output) if first_output else None
        self.idle_output = float(idle_output) if idle_output else None
        self.graceful_shutdown = float(graceful_shutdown)
        self.forced_termination = float(forced_termination)
        self.poll_interval = float(poll_interval)


def run_supervised(argv, cwd, timeout, env=None, stdin_text=None,
                   shell=False, heartbeat=None, identity=None,
                   record_file=None, first_output_timeout=None,
                   idle_output_timeout=None,
                   graceful_timeout=DEFAULT_GRACEFUL_TIMEOUT,
                   forced_timeout=DEFAULT_FORCED_TIMEOUT,
                   poll_interval=DEFAULT_POLL_INTERVAL, source="config"):
    """Run one CLI-backend command under full supervision. Never calls
    `subprocess.run`/`Popen.communicate(timeout=...)` -- the timeout is
    evaluated on our own monotonic-clock loop, independent of whatever
    the child's stdout/stderr streams are doing, so a hung child (or a
    hung grandchild still holding a pipe handle open) can never prevent
    the deadline from firing.

    Returns a dict with the same keys `execpolicy.run_command` already
    produces (`exit_code`, `stdout`, `stderr`, `timed_out`,
    `duration_seconds`, `argv`, `cwd`, `shell`, `source`) so existing
    callers (`classify_cli_failure`, etc.) need no changes, PLUS a
    `supervisor` sub-dict with the structured timeout/termination
    result (`docs/phase1-5-final-report.md` supervisor section) when a
    deadline actually fired.

    `heartbeat(dict)`, if given, is called on our own poll timer
    (default every `poll_interval` seconds) regardless of whether the
    child has produced any output -- see `wire_scheduler_heartbeat`."""
    cfg = TimeoutConfig(timeout, first_output_timeout, idle_output_timeout,
                        graceful_timeout, forced_timeout, poll_interval)
    run_env = dict(env if env is not None else os.environ)
    started_wall = _now_iso()
    started_monotonic = time.monotonic()

    popen_kwargs = dict(cwd=cwd, env=run_env, stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding="utf-8", errors="replace")
    if _IS_WINDOWS:
        popen_kwargs["creationflags"] = \
            execpolicy.windows_background_creationflags(
                subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        popen_kwargs["start_new_session"] = True

    result = {"argv": list(argv), "cwd": str(cwd), "source": source,
             "shell": bool(shell), "timed_out": False, "exit_code": None,
             "stdout": "", "stderr": "", "supervisor": None}
    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
    except FileNotFoundError:
        result["exit_code"] = 127
        result["stderr"] = "command not found: %s" % (argv[0] if argv else "")
        result["duration_seconds"] = round(
            time.monotonic() - started_monotonic, 3)
        return result

    nonce = uuid.uuid4().hex[:12]
    token = process_start_token(proc.pid)
    # absolute wall-clock point beyond which this process should
    # DEFINITELY have been reaped by its own supervisor loop if that
    # loop is still alive -- startup/operator recovery (which has no
    # live Popen handle, only this persisted record) uses this to tell
    # "still legitimately running" apart from "the owning process died
    # and left this orphaned".
    deadline_at = (_dt.datetime.now() + _dt.timedelta(
        seconds=cfg.total_execution + cfg.graceful_shutdown
        + cfg.forced_termination + 30)).isoformat(timespec="seconds")
    record = dict(identity or {})
    record.update({"pid": proc.pid, "start_time_token": token,
                   "parent_pid": os.getpid(), "nonce": nonce,
                   "created_at": started_wall, "state": SUPERVISOR_STARTING,
                   "elapsed_seconds": 0, "last_output_at": None,
                   "timeout_deadline": cfg.total_execution,
                   "deadline_at": deadline_at})
    if record_file:
        _atomic_write_json(record_file, record)

    lock = threading.Lock()
    buffers = {"stdout": [], "stderr": []}
    last_output_at = {"value": None}

    def reader(stream, key):
        try:
            for line in iter(stream.readline, ""):
                with lock:
                    buffers[key].append(line)
                    last_output_at["value"] = time.monotonic()
        except Exception:   # noqa: BLE001 -- a reader error must never
                           # hang the supervisor loop
            pass
        finally:
            try:
                stream.close()
            except Exception:   # noqa: BLE001
                pass

    if stdin_text is not None:
        def writer():
            try:
                proc.stdin.write(stdin_text)
            except Exception:   # noqa: BLE001 -- the CLI may close stdin
                               # early; never let a write error hang us
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:   # noqa: BLE001
                    pass
        threading.Thread(target=writer, daemon=True).start()
    else:
        try:
            proc.stdin.close()
        except Exception:   # noqa: BLE001
            pass

    t_out = threading.Thread(target=reader, args=(proc.stdout, "stdout"),
                             daemon=True)
    t_err = threading.Thread(target=reader, args=(proc.stderr, "stderr"),
                             daemon=True)
    t_out.start()
    t_err.start()

    state = SUPERVISOR_RUNNING
    timeout_phase = None

    def write_heartbeat():
        if not heartbeat:
            return
        with lock:
            lo = last_output_at["value"]
        heartbeat({
            "run_id": (identity or {}).get("run_id"),
            "project_id": (identity or {}).get("project_id"),
            "backend": (identity or {}).get("backend"),
            "role": (identity or {}).get("role"),
            "pid": proc.pid,
            "elapsed_seconds": round(time.monotonic() - started_monotonic, 1),
            "last_output_at": (_dt.datetime.now() -
                              _dt.timedelta(seconds=(time.monotonic() - lo))
                              ).isoformat(timespec="seconds") if lo else None,
            "timeout_deadline": cfg.total_execution,
            "supervisor_state": state,
        })

    while True:
        rc = proc.poll()
        elapsed = time.monotonic() - started_monotonic
        with lock:
            lo = last_output_at["value"]
        if record_file:
            record.update(state=state, elapsed_seconds=round(elapsed, 1),
                          last_output_at=_now_iso() if lo else None)
            _atomic_write_json(record_file, record)
        write_heartbeat()
        if rc is not None:
            break
        if lo is None and cfg.first_output and elapsed > cfg.first_output:
            timeout_phase = PHASE_FIRST_OUTPUT
            break
        if lo is not None and cfg.idle_output and \
                (time.monotonic() - lo) > cfg.idle_output:
            timeout_phase = PHASE_IDLE_OUTPUT
            break
        if elapsed > cfg.total_execution:
            timeout_phase = PHASE_TOTAL_EXECUTION
            break
        time.sleep(cfg.poll_interval)

    termination = None
    if timeout_phase is not None:
        state = SUPERVISOR_GRACEFUL_SHUTDOWN
        termination = terminate_tree(
            proc, cfg.graceful_shutdown, cfg.forced_termination,
            on_state=lambda s: None)
        state = SUPERVISOR_TIMEOUT
    else:
        state = SUPERVISOR_COMPLETED

    # bounded join: even if a stray descendant still holds a pipe handle
    # open, the supervisor itself must never hang -- whatever's been
    # captured so far is preserved as partial output for diagnostics.
    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)

    with lock:
        result["stdout"] = "".join(buffers["stdout"])
        result["stderr"] = "".join(buffers["stderr"])
    result["exit_code"] = proc.poll()
    result["timed_out"] = timeout_phase is not None
    result["duration_seconds"] = round(time.monotonic() - started_monotonic, 3)
    if timeout_phase is not None:
        result["stderr"] = (result["stderr"] +
                            "\ntimed out after %ss (phase=%s)"
                            % (round(time.monotonic() - started_monotonic, 1),
                               timeout_phase))
        result["supervisor"] = {
            "status": "timeout", "class": "execution_timeout",
            "phase": timeout_phase,
            "backend": (identity or {}).get("backend"),
            "role": (identity or {}).get("role"), "pid": proc.pid,
            "elapsed_seconds": round(time.monotonic() - started_monotonic, 1),
            "last_output_at": _now_iso() if last_output_at["value"]
            else None,
            "termination": termination,
        }

    if record_file:
        record.update(state=state, elapsed_seconds=result["duration_seconds"],
                      exit_code=result["exit_code"])
        _atomic_write_json(record_file, record)
    return result


# -- run-context binding for the default CLI runner -----------------------------
#
# One cycle invokes several CLI backend calls (conductor, coder, QA,
# security, ...) through provider adapters that only know their own
# argv/cwd/timeout/stdin -- not the surrounding run_id/project_id. Rather
# than thread five new parameters through every provider file, the
# owning cycle binds this thread-local context once
# (`_run_cycle_locked`/`project_start`) and `default_cli_runner` reads
# it to populate the process record/heartbeat. Thread-local, not a bare
# module global, so a multi-threaded host (the dashboard service can run
# cycles in worker threads) never lets one cycle's context leak into
# another's heartbeat.

def set_run_context(agentic_dir, run_id, project_id=None, task_id=None,
                    worktree=None):
    _local.context = {"agentic_dir": str(agentic_dir), "run_id": run_id,
                      "project_id": project_id, "task_id": task_id,
                      "worktree": worktree}


def clear_run_context():
    _local.context = None


def current_run_context():
    return getattr(_local, "context", None)


def default_cli_runner(argv, cwd=None, timeout=120, stdin_text=None,
                       role=None, backend=None, **_kw):
    """Drop-in replacement for the old `execpolicy.run_command`-based
    `CLIBackendBase._default_runner` -- same call signature every
    existing provider adapter already uses, so no adapter needs to
    change how it CALLS the runner, only what the runner does
    internally. When `set_run_context` has bound a cycle (the normal
    case for a real project-run), heartbeat/process-ownership records
    are written automatically; standalone calls (doctor, setup,
    ad-hoc scripts) simply get the timeout-safety fix with no
    record file."""
    ctx = current_run_context() or {}
    identity = {"run_id": ctx.get("run_id"), "project_id": ctx.get("project_id"),
               "task_id": ctx.get("task_id"), "worktree": ctx.get("worktree"),
               "backend": backend, "role": role}
    record_file = None
    if ctx.get("agentic_dir") and ctx.get("run_id"):
        record_file = record_path(ctx["agentic_dir"], ctx["run_id"],
                                  role or backend or "call",
                                  uuid.uuid4().hex[:8])
    execution_cfg_timeout = timeout
    return run_supervised(
        argv, cwd or ".", execution_cfg_timeout, stdin_text=stdin_text,
        identity=identity, record_file=record_file, source="config")


# -- startup / cycle-entry recovery (item 9) -------------------------------------

_RUNNING_STATES = (SUPERVISOR_STARTING, SUPERVISOR_RUNNING,
                  SUPERVISOR_GRACEFUL_SHUTDOWN, SUPERVISOR_FORCE_TERMINATING)


def _mark_record(record, **fields):
    path = record.get("_path")
    if not path:
        return
    clean = {k: v for k, v in record.items() if k != "_path"}
    clean.update(fields)
    _atomic_write_json(path, clean)


def recover_stale_owned_processes(agentic_dir, root=None, release_lease=None,
                                  release_lock=None, reconcile_task=None,
                                  log=None):
    """Run once at the top of every cycle (mirrors `taskspace.
    recover_abandoned`/`bootstrap_gate.recover_bootstrap_deadlock`,
    right alongside them): inspect persisted process-ownership records
    left by a run that ended without its own supervisor loop ever
    getting to clean up (the whole Python process was killed, not just
    its child) -- the ONLY scenario this module's own bounded-timeout
    guarantee can't already prevent.

    For each record still marked as running:
      - dead or PID-reused (`identity_matches` fails): reconcile now --
        never wait out a lease TTL.
      - alive but past its persisted `deadline_at`: terminate it through
        the same supervisor primitives an in-process timeout would have
        used, THEN reconcile.
      - alive and within its deadline: leave it alone and report it --
        a PID existing is never sufficient reason to kill it.

    `release_lease`/`release_lock`/`reconcile_task` are callables the
    caller supplies (project.py already owns the lease/lock/backlog
    objects) so this module never has to import project.py or guess at
    their shapes; each is optional and skipped if not provided. Returns
    a list of `{"record": ..., "action": ...}` entries for logging."""
    log = log or (lambda event: None)
    events = []
    for record in list_records(agentic_dir):
        if record.get("state") not in _RUNNING_STATES:
            continue
        matches = identity_matches(record)
        if matches:
            deadline_at = record.get("deadline_at")
            overdue = False
            if deadline_at:
                try:
                    overdue = _dt.datetime.now() > _dt.datetime.fromisoformat(
                        deadline_at)
                except ValueError:
                    overdue = False
            if not overdue:
                events.append({"record": record, "action": "left_running"})
                continue
            outcome = terminate_by_pid(record)
            log({"event": "supervisor_recovery_terminated_overdue",
                "run_id": record.get("run_id"), "pid": record.get("pid"),
                "outcome": outcome})
            _mark_record(record, state=SUPERVISOR_TIMEOUT,
                        recovery_outcome=outcome)
        else:
            log({"event": "supervisor_recovery_found_dead_process",
                "run_id": record.get("run_id"), "pid": record.get("pid")})
            _mark_record(record, state="reconciled_dead")

        run_id = record.get("run_id")
        # callables receive the full record (not just run_id) so the
        # caller can verify ownership precisely -- e.g. the project
        # lock has no run_id of its own, only the owning PID
        # (`record["parent_pid"]`), and a lease must only ever be
        # cleared when its OWN run_id still matches this record's.
        if release_lease:
            try:
                release_lease(record)
            except Exception:   # noqa: BLE001 -- reconciliation must
                               # never itself crash the cycle it's
                               # trying to unblock
                pass
        if release_lock:
            try:
                release_lock(record)
            except Exception:   # noqa: BLE001
                pass
        if reconcile_task:
            try:
                reconcile_task(record)
            except Exception:   # noqa: BLE001
                pass
        events.append({"record": record, "action": "reconciled"})
        log({"event": "supervisor_recovery_reconciled", "run_id": run_id,
            "task_id": record.get("task_id")})
    return events
