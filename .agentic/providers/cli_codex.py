"""Codex CLI backend: non-interactive `codex exec` orchestration.

- auth health check: `codex login status` (never touches ~/.codex/auth.json)
- invocation: `codex exec --ephemeral --json --sandbox <mode>
  --ask-for-approval never --cd <workspace> -` with the prompt on stdin
- sandbox: `workspace-write` only for the coder role; every other role runs
  `read-only`
- `--dangerously-bypass-approvals-and-sandbox` is on the forbidden-token list
  and can never be configured in.
- output: JSONL events; the last agent message becomes `content`, token
  usage is taken from `turn.completed` events when present.
"""
import json

from core import errors

from .cli_base import (CLIBackendBase, classify_cli_failure, compose_prompt,
                       parse_retry_hint, validate_cli_command)

WRITE_ROLES = {"coder", "worker"}


class CodexCLIBackend(CLIBackendBase):
    def __init__(self, name, cfg, runner=None, which=None, env=None):
        cfg = dict(cfg or {})
        cfg.setdefault("binary", "codex")
        cfg.setdefault("auth_probe_args", ["login", "status"])
        super().__init__(name, cfg, runner=runner, which=which, env=env)

    def sandbox_for(self, role, permissions):
        if permissions == "write" and role in WRITE_ROLES:
            return "workspace-write"
        return "read-only"

    def build_argv(self, role, permissions, workspace):
        argv = [self.binary(), "exec"]
        if self.cfg.get("ephemeral", True):
            argv.append("--ephemeral")
        argv += ["--json",
                 "--sandbox", self.sandbox_for(role, permissions),
                 "--ask-for-approval", "never"]
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
