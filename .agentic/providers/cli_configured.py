"""Configured CLI backend for Claude Code, Qwen coding CLI, and future
authenticated CLIs.

Nothing about these CLIs is assumed: the exact invocation lives in
configuration and is validated during setup (version detection + a
non-interactive smoke test are required before the backend is marked usable).
Interactive TUIs are never automated via keystroke injection — the configured
command must be a genuine non-interactive mode (e.g. `claude -p`, `qwen -p`).

Config shape (per backend, in config.yaml / config.machine.yaml):

  backends:
    claude:
      type: cli
      kind: configured
      binary: claude
      version_args: ["--version"]
      auth_probe_args: null              # optional safe status command
      invoke_args: ["-p", "--output-format", "json"]
      write_args: ["--permission-mode", "acceptEdits"]   # coder role only
      read_args: []
      prompt_via: stdin                  # stdin | arg
      parse: auto                        # auto | json | text
      model: auto                        # auto/missing/placeholder -> no
                                          # model flag; the authenticated
                                          # CLI (e.g. Claude Code) picks its
                                          # own subscription default
      model_flag: --model                # flag used when model IS explicit
"""
from core import errors
from core.modelres import resolve_model

from .cli_base import (CLIBackendBase, classify_cli_failure, compose_prompt,
                       extract_first_json, parse_retry_hint,
                       validate_cli_command)

WRITE_ROLES = {"coder", "worker"}


class ConfiguredCLIBackend(CLIBackendBase):
    def __init__(self, name, cfg, runner=None, which=None, env=None):
        super().__init__(name, cfg, runner=runner, which=which, env=env)
        self.last_model_resolution = None

    def build_argv(self, role, permissions):
        argv = [self.binary()] + [str(a) for a in
                                  self.cfg.get("invoke_args", [])]
        resolution = resolve_model(role, "cli", self.name,
                                   backend_model=self.cfg.get("model"))
        self.last_model_resolution = resolution
        if resolution["model_flag_emitted"]:
            argv += [str(self.cfg.get("model_flag", "--model")),
                     resolution["resolved_model"]]
        if permissions == "write" and role in WRITE_ROLES:
            argv += [str(a) for a in self.cfg.get("write_args", [])]
        else:
            argv += [str(a) for a in self.cfg.get("read_args", [])]
        return argv

    def invoke(self, role, prompt, input_data, workspace, permissions,
               timeout):
        full_prompt = compose_prompt(prompt, input_data)
        argv = self.build_argv(role, permissions)
        stdin_text = None
        if self.cfg.get("prompt_via", "stdin") == "arg":
            argv.append(full_prompt)
        else:
            stdin_text = full_prompt
        argv = validate_cli_command(argv)
        run = self.runner(argv, cwd=workspace, timeout=timeout,
                          stdin_text=stdin_text)
        output = run["stdout"] + "\n" + run["stderr"]
        if run["timed_out"] or run["exit_code"] != 0:
            classify_cli_failure(self.name, run["exit_code"], output,
                                 run["timed_out"])
        content, usage, model = self._parse(run["stdout"])
        if not (content or "").strip():
            raise errors.MalformedOutputError("CLI produced no content",
                                              provider=self.name)
        retry_after, reset_at = parse_retry_hint(output)
        return self.normalize(role, content, usage, model=model,
                              exit_code=run["exit_code"],
                              capacity={"remaining_reported": None,
                                        "reset_at": reset_at,
                                        "retry_after_seconds": retry_after})

    def _parse(self, stdout):
        mode = self.cfg.get("parse", "auto")
        if mode == "text":
            return stdout, {}, None
        obj = extract_first_json(stdout or "")
        if isinstance(obj, dict):
            content = obj.get("result") or obj.get("content") or \
                obj.get("text") or obj.get("response")
            usage_raw = obj.get("usage") or {}
            usage = {
                "input_tokens": usage_raw.get("input_tokens"),
                "cached_input_tokens": usage_raw.get(
                    "cache_read_input_tokens",
                    usage_raw.get("cached_input_tokens")),
                "output_tokens": usage_raw.get("output_tokens"),
                "reasoning_tokens": usage_raw.get("reasoning_tokens"),
                "estimated": False,
            } if usage_raw else {}
            model = obj.get("model")
            if isinstance(content, str) and content.strip():
                return content, usage, model
        if mode == "json":
            return None, {}, None
        return stdout, {}, None   # auto: fall back to raw text
