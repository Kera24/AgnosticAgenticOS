"""Base for authenticated subscription-CLI backends (Codex, Claude Code,
Qwen, future CLIs).

Authentication is entirely the CLI's own business: adapters only execute the
CLI binary. They never read, copy, print, upload, modify, or commit cached
credential files — configured commands are validated against a forbidden-token
list before anything runs. Health checks use the CLI's own supported
status/diagnostic commands.
"""
import datetime as _dt
import json
import re
import shutil

from core import errors
from core import execpolicy
from core.jsonx import extract_first_json

# Any configured CLI command containing one of these is refused outright.
FORBIDDEN_COMMAND_TOKENS = [
    "auth.json", ".codex/auth", ".claude/.credentials", "credentials.json",
    "--dangerously-bypass-approvals-and-sandbox",
]

_RETRY_IN_RE = re.compile(
    r"(?:retry|try again|resets?|available)\D{0,20}?(\d+(?:\.\d+)?)\s*"
    r"(seconds?|secs?|s\b|minutes?|mins?|m\b|hours?|hrs?|h\b)", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r"retry-after[:\s]+(\d+)", re.IGNORECASE)
_RESET_AT_RE = re.compile(
    r"resets?\s+(?:at\s+)?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)",
    re.IGNORECASE)

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}


def parse_retry_hint(text):
    """Extract (retry_after_seconds, reset_at_iso) from CLI/provider output.
    Returns (None, None) when nothing explicit is present — never invented."""
    if not text:
        return None, None
    m = _RETRY_AFTER_RE.search(text)
    if m:
        return int(m.group(1)), None
    m = _RETRY_IN_RE.search(text)
    if m:
        unit = m.group(2)[0].lower()
        return int(float(m.group(1)) * _UNIT_SECONDS.get(unit, 1)), None
    m = _RESET_AT_RE.search(text)
    if m:
        return None, m.group(1).replace(" ", "T")
    return None, None


def classify_cli_failure(name, exit_code, output, timed_out=False):
    """Map CLI process results onto the typed error taxonomy."""
    low = (output or "").lower()
    retry_after, reset_at = parse_retry_hint(output)
    if timed_out:
        raise errors.TimeoutError_("CLI timed out", provider=name)
    if exit_code == 127 or "command not found" in low or "not recognized" in low:
        raise errors.BackendUnavailableError("CLI not installed", provider=name)
    if any(t in low for t in ("usage limit", "usage_limit", "quota exceeded",
                              "out of credits", "usage cap", "limit reached")):
        raise errors.UsageLimitError(output[-300:], provider=name,
                                     retry_after_seconds=retry_after,
                                     reset_at=reset_at)
    if "rate limit" in low or "too many requests" in low or " 429" in low:
        err = errors.RateLimitError(output[-300:], provider=name)
        err.retry_after_seconds = retry_after
        err.reset_at = reset_at
        raise err
    if any(t in low for t in ("not logged in", "please log in", "please login",
                              "unauthorized", "401", "login required",
                              "authentication required", "no api key")):
        raise errors.AuthError(output[-300:], provider=name)
    if "permission" in low and "denied" in low or "approval required" in low:
        raise errors.PermissionDeniedError(output[-300:], provider=name)
    if "context" in low and any(t in low for t in ("length", "window",
                                                   "too long", "too large")):
        raise errors.ContextLengthError(output[-300:], provider=name)
    if "model not found" in low or "unknown model" in low:
        raise errors.ModelUnavailableError(output[-300:], provider=name)
    if exit_code is None or exit_code < 0:
        raise errors.InterruptedProcessError("exit=%s" % exit_code, provider=name)
    raise errors.UnknownFailureError("exit=%s: %s" % (exit_code, output[-300:]),
                                     provider=name)


def validate_cli_command(argv):
    joined = " ".join(str(a) for a in argv).lower()
    for token in FORBIDDEN_COMMAND_TOKENS:
        if token in joined:
            raise errors.PolicyError(
                "CLI command refused (forbidden token %r)" % token)
    return argv


class CLIBackendBase:
    backend_type = "cli"
    capabilities = {"tool_calling": True, "structured_output": False,
                    "usage_reporting": False, "refusal_reporting": False,
                    "reasoning_control": False, "context_window": None}

    def __init__(self, name, cfg, runner=None, which=None, env=None):
        self.name = name
        self.cfg = cfg or {}
        self.runner = runner or self._default_runner
        self.which = which or shutil.which
        self.cost_free = True  # subscription/local: no per-token USD cost

    @staticmethod
    def _default_runner(argv, cwd=None, timeout=120, stdin_text=None):
        return execpolicy.run_command(argv, cwd=cwd or ".", timeout=timeout,
                                      source="config", stdin_text=stdin_text)

    def binary(self):
        return self.cfg.get("binary", self.name)

    def detect(self):
        path = self.which(self.binary())
        if not path:
            return {"installed": False, "version": None, "path": None}
        version_args = self.cfg.get("version_args", ["--version"])
        run = self.runner(validate_cli_command([self.binary()] + list(version_args)),
                          timeout=30)
        version = (run["stdout"] or run["stderr"]).strip().splitlines()
        return {"installed": True, "path": path,
                "version": version[0][:80] if version else None}

    def auth_status(self):
        """'ok' | 'required' | 'unknown' — via the CLI's own status command;
        credential files are never inspected."""
        probe = self.cfg.get("auth_probe_args")
        if not probe:
            return "unknown"
        run = self.runner(validate_cli_command([self.binary()] + list(probe)),
                          timeout=30)
        output = (run["stdout"] + run["stderr"]).lower()
        if run["exit_code"] == 0 and "not logged in" not in output:
            return "ok"
        if any(t in output for t in ("not logged in", "login", "unauthorized")):
            return "required"
        return "unknown"

    def smoke_test(self, workspace):
        """Non-interactive smoke test; must succeed before the backend is
        marked usable for autonomous operation."""
        try:
            result = self.invoke("smoke", "Reply with exactly: OK", None,
                                 workspace=workspace, permissions="read",
                                 timeout=int(self.cfg.get("smoke_timeout", 120)))
            return bool(result.get("ok"))
        except errors.AgenticError:
            return False

    def normalize(self, role, content, usage=None, model=None,
                  finish_reason="completed", exit_code=0, capacity=None):
        usage = usage or {}
        return {
            "ok": True, "backend": self.name, "backend_type": self.backend_type,
            "model": model, "role": role,
            "provider": self.name,  # legacy key kept for ledgers
            "content": content or "", "structured_output": {},
            "usage": {
                "input_tokens": usage.get("input_tokens"),
                "cached_input_tokens": usage.get("cached_input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "reasoning_tokens": usage.get("reasoning_tokens"),
                "estimated": bool(usage.get("estimated", not usage)),
            },
            "capacity": capacity or {"remaining_reported": None,
                                     "reset_at": None,
                                     "retry_after_seconds": None},
            "finish_reason": finish_reason, "refusal": False,
            "exit_code": exit_code, "estimated_cost_usd": 0.0, "error": None,
        }

    def invoke(self, role, prompt, input_data, workspace, permissions,
               timeout):
        raise NotImplementedError


def compose_prompt(prompt, input_data):
    # Subscription CLIs get one flat prompt. The broker's cache-boundary
    # marker is stripped: we make no claim of controlling provider-side
    # caching for CLIs — the stable prefix ordering is still consistent,
    # and cache status remains honestly "unknown".
    from core.context.broker import strip_cache_boundary
    prompt = strip_cache_boundary(prompt)
    if input_data is None:
        return prompt
    if not isinstance(input_data, str):
        input_data = json.dumps(input_data, ensure_ascii=False, indent=2)
    return prompt + "\n\n# INPUT DATA\n" + input_data


def now_iso():
    return _dt.datetime.now().isoformat(timespec="seconds")


__all__ = ["CLIBackendBase", "classify_cli_failure", "parse_retry_hint",
           "validate_cli_command", "compose_prompt", "extract_first_json",
           "FORBIDDEN_COMMAND_TOKENS"]
