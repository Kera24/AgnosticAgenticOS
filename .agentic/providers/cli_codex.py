"""Codex CLI backend: non-interactive `codex exec` orchestration.

- auth health check: `codex login status` (never touches ~/.codex/auth.json)
- invocation: `codex -a never exec --ignore-user-config --ephemeral --json
  --sandbox <mode> --cd <workspace> -` with the prompt on stdin. Approval
  configuration is a GLOBAL option on this CLI version and must be placed
  before the `exec` subcommand (`codex -a never exec ...`), never after it
  (`codex exec --ask-for-approval never` is refused unless capability
  detection/config explicitly says the installed version needs it) --
  see `approval_placement` in cfg for the escape hatch.
- sandbox: `workspace-write` only for the coder role; every other role runs
  `read-only`
- `--dangerously-bypass-approvals-and-sandbox` is on the forbidden-token list
  and can never be configured in.
- output: JSONL events, one independent JSON object per non-empty line;
  the last agent message becomes `content`, token usage is taken from
  `turn.completed` events when present. A terminal `error` or `turn.failed`
  event fails the call even when the process exit code is 0.
- smoke test: a dedicated, capability-probed (`codex --help` /
  `codex exec --help`) read-only invocation matching the confirmed-working
  manual command exactly -- prompt as a positional argument, no stdin, no
  --cd, --sandbox read-only only. Pass/fail is decided from exit code,
  timeout, and terminal JSONL events only; non-empty stderr alone is never
  a failure.
"""
import json
import re

from core import errors

from .cli_base import (CLIBackendBase, classify_cli_failure, compose_prompt,
                       parse_retry_hint, validate_cli_command)

WRITE_ROLES = {"coder", "worker"}
SMOKE_MARKER = "CODEX_SMOKE_OK"


def _parse_events(stdout):
    """Tolerant JSONL parse: each non-empty line is an independent JSON
    object. Non-JSON lines (banners, progress text) are skipped rather than
    failing the whole parse."""
    events = []
    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _terminal_error(events):
    """First terminal `error` or `turn.failed` event, or None. Position in
    the stream does not matter -- either event type fails the call."""
    for event in events:
        etype = event.get("type")
        if etype == "error":
            return str(event.get("message") or event.get("error") or
                       event)[:200]
        if etype == "turn.failed":
            return str(event.get("error") or event.get("message") or
                       event)[:200]
    return None


def evaluate_smoke_jsonl(stdout, exit_code, timed_out, marker=SMOKE_MARKER):
    """Pure smoke-test verdict from raw stdout + process result. Never
    requires a single JSON object, and never requires the marker or
    turn.completed to appear on any particular (first/last) line.

    Passes only when: exit code is zero, no terminal `error`/`turn.failed`
    event exists, an `item.completed` event carries an `agent_message`
    whose text contains `marker`, and a `turn.completed` event exists."""
    events = _parse_events(stdout)
    event_types = [e.get("type") for e in events if e.get("type")]

    if timed_out:
        return {"ok": False, "reason": "timeout", "event_types": event_types}
    if exit_code != 0:
        return {"ok": False, "reason": "nonzero exit code: %s" % exit_code,
                "event_types": event_types}

    terminal_error = _terminal_error(events)
    if terminal_error:
        return {"ok": False,
                "reason": "terminal error event: %s" % terminal_error,
                "event_types": event_types}

    got_marker = False
    got_turn_completed = False
    for event in events:
        etype = event.get("type")
        if etype == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and \
                    marker in (item.get("text") or ""):
                got_marker = True
        elif etype == "turn.completed":
            got_turn_completed = True

    if not got_marker:
        return {"ok": False,
                "reason": "no item.completed agent_message containing %r"
                          % marker,
                "event_types": event_types}
    if not got_turn_completed:
        return {"ok": False, "reason": "no turn.completed event",
                "event_types": event_types}
    return {"ok": True, "reason": "pass", "event_types": event_types}


def _redact_argv(argv):
    """Command structure for diagnostics with the prompt scrubbed -- never
    logs/prints the actual prompt text."""
    redacted = list(argv)
    if redacted:
        redacted[-1] = "<prompt redacted>"
    return redacted


class CodexCLIBackend(CLIBackendBase):
    def __init__(self, name, cfg, runner=None, which=None, env=None):
        cfg = dict(cfg or {})
        cfg.setdefault("binary", "codex")
        cfg.setdefault("auth_probe_args", ["login", "status"])
        super().__init__(name, cfg, runner=runner, which=which, env=env)
        self._caps = None
        self.last_smoke = None

    def sandbox_for(self, role, permissions):
        if permissions == "write" and role in WRITE_ROLES:
            return "workspace-write"
        return "read-only"

    # -- capability probing ------------------------------------------------
    # Used by the smoke test (which can afford the extra `--help` calls) and
    # by setup/doctor diagnostics. NOT consulted on the production invoke()
    # hot path: that path uses the confirmed-working default (global `-a`)
    # with a config escape hatch, so every real task doesn't pay for two
    # extra subprocess spawns.
    def capabilities(self):
        if self._caps is not None:
            return self._caps
        global_help = self._help_text([])
        exec_help = self._help_text(["exec"])
        text = global_help + "\n" + exec_help
        probed = bool(text.strip())
        self._caps = {
            "probed": probed,
            "approval_global": (not probed) or bool(
                re.search(r"(^|[\s,])-a\b|--ask-for-approval", global_help)),
            "approval_subcommand": "--ask-for-approval" in exec_help,
            "ignore_user_config": (not probed) or
                                  "--ignore-user-config" in text,
            "ephemeral": (not probed) or "--ephemeral" in text,
            "json": (not probed) or "--json" in text,
            "sandbox_values": self._sandbox_values(text) if probed else
                              ["read-only", "workspace-write",
                               "danger-full-access"],
        }
        return self._caps

    def _help_text(self, extra_args):
        try:
            run = self.runner(
                validate_cli_command([self.binary()] + list(extra_args) +
                                     ["--help"]), timeout=15)
        except Exception:
            return ""
        if run.get("timed_out"):
            return ""
        return (run.get("stdout") or "") + "\n" + (run.get("stderr") or "")

    @staticmethod
    def _sandbox_values(text):
        m = re.search(r"sandbox[^\[]*\[possible values:\s*([^\]]+)\]",
                      text, re.IGNORECASE | re.DOTALL)
        if not m:
            return ["read-only", "workspace-write", "danger-full-access"]
        return [v.strip() for v in m.group(1).split(",") if v.strip()]

    def _probed_approval_args(self, caps):
        if caps.get("probed") and caps.get("approval_subcommand") and \
                not caps.get("approval_global"):
            return [], ["--ask-for-approval", "never"]
        return ["-a", "never"], []

    # -- production invocation ----------------------------------------------
    def build_argv(self, role, permissions, workspace):
        if self.cfg.get("approval_placement") == "subcommand":
            argv = [self.binary(), "exec", "--ask-for-approval", "never"]
        else:
            argv = [self.binary(), "-a", "never", "exec"]
        if self.cfg.get("ignore_user_config", True):
            argv.append("--ignore-user-config")
        if self.cfg.get("ephemeral", True):
            argv.append("--ephemeral")
        argv += ["--json",
                 "--sandbox", self.sandbox_for(role, permissions)]
        if workspace:
            argv += ["--cd", str(workspace)]
        if self.cfg.get("model"):
            argv += ["--model", str(self.cfg["model"])]
        argv += list(self.cfg.get("extra_args", []))
        argv.append("-")   # prompt arrives on stdin
        return validate_cli_command(argv)

    def invoke(self, role, prompt, input_data, workspace, permissions,
               timeout):
        argv = self.build_argv(role, permissions, workspace)
        run = self.runner(argv, cwd=workspace, timeout=timeout,
                          stdin_text=compose_prompt(prompt, input_data))
        output = run["stdout"] + "\n" + run["stderr"]
        if run["timed_out"] or run["exit_code"] != 0:
            classify_cli_failure(self.name, run["exit_code"], output,
                                 run["timed_out"])
        terminal_error = _terminal_error(_parse_events(run["stdout"]))
        if terminal_error:
            raise errors.UnknownFailureError(
                "codex reported a terminal error event: %s" % terminal_error,
                provider=self.name)
        content, usage, model = self.parse_jsonl(run["stdout"])
        if content is None:
            raise errors.MalformedOutputError(
                "no agent message found in codex JSONL output",
                provider=self.name)
        retry_after, reset_at = parse_retry_hint(output)
        return self.normalize(role, content, usage, model=model,
                              exit_code=run["exit_code"],
                              capacity={"remaining_reported": None,
                                        "reset_at": reset_at,
                                        "retry_after_seconds": retry_after})

    def parse_jsonl(self, stdout):
        """Tolerant JSONL parse: collect the last agent/assistant message and
        any usage block. Codex event names have shifted across versions, so
        match on shape, not exact type strings."""
        content, usage, model = None, {}, None
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            model = event.get("model") or model
            item = event.get("item") or event.get("msg") or event
            if isinstance(item, dict):
                if item.get("type") in ("agent_message", "assistant_message",
                                        "message") or "text" in item:
                    text = item.get("text") or item.get("content") or \
                        item.get("message")
                    if isinstance(text, str) and text.strip():
                        content = text
            u = event.get("usage")
            if not isinstance(u, dict) and isinstance(event.get("info"), dict):
                u = event["info"].get("usage")
            if isinstance(u, dict):
                usage = {
                    "input_tokens": u.get("input_tokens"),
                    "cached_input_tokens": u.get("cached_input_tokens",
                                                 u.get("cached_tokens")),
                    "output_tokens": u.get("output_tokens"),
                    "reasoning_tokens": u.get("reasoning_output_tokens",
                                              u.get("reasoning_tokens")),
                    "estimated": False,
                }
        return content, usage, model

    # -- smoke test -----------------------------------------------------------
    def build_smoke_argv(self, marker=SMOKE_MARKER, sandbox="read-only"):
        """The exact shape of the confirmed-working manual command:
        `codex -a never exec --ignore-user-config --ephemeral --json
        --sandbox read-only "Reply with exactly: <marker>"` -- capability
        probed so flags this CLI version doesn't support are simply
        omitted rather than guessed."""
        caps = self.capabilities()
        pre, post = self._probed_approval_args(caps)
        argv = [self.binary()] + pre + ["exec"] + post
        if caps.get("ignore_user_config", True):
            argv.append("--ignore-user-config")
        if caps.get("ephemeral", True):
            argv.append("--ephemeral")
        if caps.get("json", True):
            argv.append("--json")
        argv += ["--sandbox", sandbox]
        argv.append("Reply with exactly: %s" % marker)
        return validate_cli_command(argv)

    def smoke_test(self, workspace):
        """Non-interactive, read-only smoke test. Prompt travels as a
        positional argument (matching the confirmed-working manual
        invocation exactly), not stdin. Failure is decided from exit code,
        timeout, and terminal JSONL events only -- non-empty stderr alone
        (Codex uses it for progress/diagnostics) is never a failure."""
        argv = self.build_smoke_argv()
        timeout = int(self.cfg.get("smoke_timeout", 120))
        try:
            run = self.runner(argv, cwd=workspace, timeout=timeout)
        except Exception as exc:
            self.last_smoke = {"ok": False,
                               "reason": "runner error: %s" % str(exc)[:200],
                               "event_types": [],
                               "argv": _redact_argv(argv), "cwd": str(workspace)}
            return False
        verdict = evaluate_smoke_jsonl(run.get("stdout", ""),
                                       run.get("exit_code"),
                                       run.get("timed_out", False))
        self.last_smoke = dict(verdict, argv=_redact_argv(argv),
                               exit_code=run.get("exit_code"),
                               timed_out=run.get("timed_out", False),
                               cwd=str(workspace))
        return verdict["ok"]

    def readiness_report(self, workspace=None):
        """Sanitised diagnostics for setup/doctor: version, installed,
        auth, smoke-test command structure (prompt redacted), working
        directory, exit code, timeout, JSONL event types, and autonomous
        readiness. Never includes credentials, tokens, or file contents."""
        info = self.detect()
        auth = self.auth_status()
        smoke = self.last_smoke
        return {
            "backend": self.name,
            "installed": info.get("installed", False),
            "version": info.get("version"),
            "auth_status": auth,
            "smoke": smoke,
            "autonomous_ready": bool(
                info.get("installed") and auth == "ok" and
                smoke and smoke.get("ok")),
        }
