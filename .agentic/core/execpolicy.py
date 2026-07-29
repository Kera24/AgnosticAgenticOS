"""Command-execution policy: the single choke point for running external
commands.

Rules enforced here (not in prompts):
- Commands are argument arrays. String commands from configuration are split
  with shlex; `shell=True` is used ONLY when an administrator explicitly set
  `shell_required: true` on that configured command.
- Commands originating from model output (`source="model"`) are NEVER run
  with a shell and must match the configured allowlist verbatim.
- Every execution records the exact argv, exit code, and duration.
"""
import os
import re
import shlex
import shutil
import subprocess
import time

from . import errors

# Capture the real resolver once. Other modules legitimately monkeypatch the
# shared shutil module in isolated tests; those patches must not redirect
# unrelated verification commands.
_DEFAULT_WHICH = shutil.which

# Never a permitted command on Windows regardless of configuration (item
# 7 of the canonical-contract-divergence fix): `rm -rf`/`-fr` is a
# Unix-shell recursive-delete idiom an architect/admin might carry over
# from Linux-centric examples -- inert here even if it somehow ends up
# in an allowlist, on every OS this platform runs `run_allowlisted` on
# when that OS is Windows.
_RM_RF_RE = re.compile(r"^\s*rm\s+(-\S*\s+)*-[a-z]*[rf][a-z]*[rf][a-z]*\b",
                      re.I)


def parse_command(cmd):
    """Normalise a configured command into an argv list."""
    if isinstance(cmd, (list, tuple)):
        return [str(part) for part in cmd]
    if isinstance(cmd, str):
        return shlex.split(cmd, posix=True)
    raise errors.PolicyError("unsupported command type: %r" % type(cmd))



def resolve_argv_executable(argv, env=None, platform=None, which=None):
    """Resolve a bare executable on native Windows without enabling a shell.

    Windows developer tools installed through npm are commonly exposed as
    npm.cmd/npx.cmd. CreateProcess does not reliably resolve a bare npm token
    when invoked through Python with shell=False. Resolve only argv[0] through
    PATH/PATHEXT after policy/allowlist matching; explicit paths and every
    argument after argv[0] remain byte-for-byte unchanged. Missing commands
    remain unchanged so the normal, typed FileNotFoundError path still reports
    exit 127.
    """
    resolved = list(argv or [])
    platform = platform or os.name
    if platform != "nt" or not resolved:
        return resolved
    command = resolved[0]
    if os.path.dirname(command):
        return resolved
    which = which or _DEFAULT_WHICH
    search_path = (env or os.environ).get("PATH")
    found = which(command, path=search_path)
    if found:
        resolved[0] = found
    return resolved


def windows_background_creationflags(existing=0, platform=None,
                                     show_child_windows=False):
    """Return Windows flags for a captured, non-interactive child process.

    Agentic OS captures stdout/stderr and surfaces them through logs and the
    UI, so verification tools and CLI agents do not need a visible console.
    CREATE_NO_WINDOW prevents disruptive cmd/PowerShell flashes. The explicit
    switch remains available to developers debugging a child interactively.
    """
    platform = platform or os.name
    if platform != "nt" or show_child_windows:
        return existing
    return existing | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def run_command(cmd, cwd, timeout, env=None, shell_required=False,
                source="config", stdin_text=None, extra_env=None):
    """Execute one command under policy. Returns a result dict; raises
    PolicyError when the request violates policy (never executes then)."""
    if source == "model" and shell_required:
        raise errors.PolicyError("model-originated commands may never use a shell")
    if shell_required and not isinstance(cmd, str):
        raise errors.PolicyError("shell_required commands must be admin-authored strings")

    run_env = dict(env if env is not None else os.environ)
    run_env.update(extra_env or {})

    if shell_required:
        popen_cmd, use_shell = cmd, True
        argv_logged = ["<shell>", cmd]
    else:
        argv_logged = parse_command(cmd)
        popen_cmd = resolve_argv_executable(argv_logged, env=run_env)
        use_shell = False

    started = time.time()
    result = {"argv": argv_logged, "cwd": str(cwd), "source": source,
              "shell": use_shell, "timed_out": False, "exit_code": None,
              "stdout": "", "stderr": ""}
    try:
        # encoding must be explicit: without it, Python 3's subprocess falls
        # back to locale.getpreferredencoding() for both stdin and captured
        # output. On Windows that's typically a codepage (e.g. cp1252), not
        # UTF-8 -- encoding a prompt containing any character outside that
        # codepage silently produces bytes that are NOT valid UTF-8, which a
        # CLI expecting a UTF-8 stdin stream (Codex, Claude Code, ...)
        # correctly rejects ("input is not valid UTF-8"). Every argv/prompt
        # this policy sends is UTF-8 by construction, so force it both ways.
        proc = subprocess.run(
            popen_cmd, shell=use_shell, cwd=cwd, capture_output=True,
            text=True, timeout=timeout, env=run_env, input=stdin_text,
            encoding="utf-8", errors="replace",
            creationflags=windows_background_creationflags())
        result["exit_code"] = proc.returncode
        result["stdout"] = proc.stdout or ""
        result["stderr"] = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        result["timed_out"] = True
        result["stdout"] = (exc.stdout or b"").decode("utf-8", "replace") \
            if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        result["stderr"] = "timed out after %ss" % timeout
    except FileNotFoundError:
        result["exit_code"] = 127
        result["stderr"] = "command not found: %s" % argv_logged[0]
    result["duration_seconds"] = round(time.time() - started, 3)
    return result


def run_allowlisted(cmd, allowlist, cwd, timeout, env=None):
    """Run a model-requested command: must appear verbatim in the allowlist;
    executed without a shell. Returns None (skipped) if not allowlisted."""
    if os.name == "nt" and _RM_RF_RE.search(str(cmd)):
        return None   # never a permitted command on Windows -- see module docstring
    if cmd not in (allowlist or []):
        return None
    return run_command(cmd, cwd, timeout, env=env, shell_required=False,
                       source="model")
