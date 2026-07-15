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
import shlex
import subprocess
import time

from . import errors


def parse_command(cmd):
    """Normalise a configured command into an argv list."""
    if isinstance(cmd, (list, tuple)):
        return [str(part) for part in cmd]
    if isinstance(cmd, str):
        return shlex.split(cmd, posix=True)
    raise errors.PolicyError("unsupported command type: %r" % type(cmd))


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
        popen_cmd, use_shell = parse_command(cmd), False
        argv_logged = popen_cmd

    started = time.time()
    result = {"argv": argv_logged, "cwd": str(cwd), "source": source,
              "shell": use_shell, "timed_out": False, "exit_code": None,
              "stdout": "", "stderr": ""}
    try:
        proc = subprocess.run(popen_cmd, shell=use_shell, cwd=cwd,
                              capture_output=True, text=True, timeout=timeout,
                              env=run_env, input=stdin_text)
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
    if cmd not in (allowlist or []):
        return None
    return run_command(cmd, cwd, timeout, env=env, shell_required=False,
                       source="model")
