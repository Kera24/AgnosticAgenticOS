"""Escape hatch: drive any local CLI as a model. The command receives a JSON
request on stdin and must print a JSON object on stdout:

  in:  {"model": ..., "prompt": ..., "input": ..., "max_output_tokens": ...}
  out: {"content": "...", "usage": {"input_tokens": 0, "output_tokens": 0}}

Anything else on stdout is tolerated if the first JSON object parses.
"""
import json
import subprocess

from .base import BaseProvider, detect_refusal
from core import errors
from core.jsonx import extract_first_json


class CustomCommandProvider(BaseProvider):
    capabilities = {
        "tool_calling": False,
        "structured_output": False,
        "usage_reporting": False,
        "refusal_reporting": False,
        "reasoning_control": False,
        "context_window": None,
    }

    def invoke(self, model, prompt, input_data=None, tools=None, timeout=120,
               max_output_tokens=None, temperature=0):
        command = self.cfg.get("command")
        if not command:
            raise errors.ProviderError("custom_command provider has no command",
                                       provider=self.name, model=model)
        if isinstance(command, str):
            command = command.split()
        from core.context.broker import strip_cache_boundary
        request = json.dumps({
            "model": model, "prompt": strip_cache_boundary(prompt),
            "input": input_data,
            "max_output_tokens": max_output_tokens, "temperature": temperature,
        })
        try:
            proc = subprocess.run(command, input=request, capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise errors.TimeoutError_("command timed out after %ss" % timeout,
                                       provider=self.name, model=model)
        except FileNotFoundError:
            raise errors.ModelUnavailableError("command not found: %s" % command[0],
                                               provider=self.name, model=model)
        if proc.returncode != 0:
            raise errors.ProviderError(
                "command exited %d: %s" % (proc.returncode, proc.stderr[:300]),
                provider=self.name, model=model)
        data = extract_first_json(proc.stdout)
        if data is None or "content" not in data:
            raise errors.ProviderError("command produced no JSON with 'content'",
                                       provider=self.name, model=model)
        usage = data.get("usage") or {}
        content = data.get("content") or ""
        return self.normalize(model, content, usage,
                              data.get("finish_reason", "stop"),
                              detect_refusal(content))
