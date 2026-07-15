"""Anthropic Messages API adapter. Plain HTTP; no SDK."""
import json

from .base import BaseProvider, detect_refusal
from core import errors

_FINISH_MAP = {"end_turn": "stop", "max_tokens": "length",
               "stop_sequence": "stop", "refusal": "refusal"}


class AnthropicProvider(BaseProvider):
    capabilities = {
        "tool_calling": True,
        "structured_output": False,   # JSON requested via prompt, validated locally
        "usage_reporting": True,
        "refusal_reporting": True,
        "reasoning_control": True,
        "context_window": None,
    }
    DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
    API_VERSION = "2023-06-01"

    def invoke(self, model, prompt, input_data=None, tools=None, timeout=120,
               max_output_tokens=None, temperature=0):
        if input_data is not None and not isinstance(input_data, str):
            input_data = json.dumps(input_data, ensure_ascii=False, indent=2)
        payload = {
            "model": model,
            "max_tokens": int(max_output_tokens or 4096),
            "temperature": temperature,
        }
        if input_data is not None:
            payload["system"] = prompt
            payload["messages"] = [{"role": "user", "content": input_data}]
        else:
            payload["messages"] = [{"role": "user", "content": prompt}]
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key(required=True),
            "anthropic-version": self.API_VERSION,
        }
        url = self.base_url(self.DEFAULT_BASE_URL) + "/messages"
        status, text = self.transport(url, headers,
                                      json.dumps(payload).encode("utf-8"), timeout)
        if status != 200:
            self._map_anthropic_error(status, text, model)
        data = self.parse_json_body(text, model)
        blocks = data.get("content") or []
        content = "".join(b.get("text", "") for b in blocks
                          if isinstance(b, dict) and b.get("type") == "text")
        stop_reason = data.get("stop_reason") or "end_turn"
        finish = _FINISH_MAP.get(stop_reason, stop_reason)
        usage_raw = data.get("usage") or {}
        usage = {
            "input_tokens": usage_raw.get("input_tokens", 0),
            "output_tokens": usage_raw.get("output_tokens", 0),
            "cached_tokens": usage_raw.get("cache_read_input_tokens", 0),
        }
        refusal = detect_refusal(content, finish, stop_reason == "refusal")
        return self.normalize(data.get("model", model), content, usage,
                              finish, refusal)

    def _map_anthropic_error(self, status, text, model):
        low = (text or "").lower()
        if status == 400 and ("prompt is too long" in low or "context" in low):
            raise errors.ContextLengthError(text[:400], provider=self.name, model=model)
        if status == 404 or "not_found_error" in low:
            raise errors.ModelUnavailableError(text[:400], provider=self.name, model=model)
        if status == 529:
            raise errors.ProviderError("overloaded: " + text[:200],
                                       provider=self.name, model=model)
        self.map_http_error(status, text, model)
