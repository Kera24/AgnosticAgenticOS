"""OpenAI Chat Completions adapter (also the base for every
OpenAI-compatible endpoint). Uses plain HTTP via the shared transport; no SDK."""
import json

from .base import BaseProvider, detect_refusal
from core import errors


class OpenAIProvider(BaseProvider):
    capabilities = {
        "tool_calling": True,
        "structured_output": True,
        "usage_reporting": True,
        "refusal_reporting": True,
        "reasoning_control": True,
        "context_window": None,
    }
    DEFAULT_BASE_URL = "https://api.openai.com/v1"

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        key = self.api_key(required=True)
        if key:
            headers["Authorization"] = "Bearer " + key
        return headers

    def _messages(self, prompt, input_data):
        messages = [{"role": "system", "content": prompt}]
        if input_data is not None:
            if not isinstance(input_data, str):
                input_data = json.dumps(input_data, ensure_ascii=False, indent=2)
            messages.append({"role": "user", "content": input_data})
        else:
            messages = [{"role": "user", "content": prompt}]
        return messages

    def invoke(self, model, prompt, input_data=None, tools=None, timeout=120,
               max_output_tokens=None, temperature=0):
        payload = {
            "model": model,
            "messages": self._messages(prompt, input_data),
            "temperature": temperature,
        }
        if max_output_tokens:
            payload["max_tokens"] = int(max_output_tokens)
        url = self.base_url(self.DEFAULT_BASE_URL) + "/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        status, text = self.transport(url, self._headers(), body, timeout)
        if status != 200:
            self.map_http_error(status, text, model)
        data = self.parse_json_body(text, model)
        return self._parse_completion(data, model)

    def _parse_completion(self, data, model):
        choices = data.get("choices") or []
        if not choices:
            err = data.get("error") or {}
            raise errors.ProviderError(
                "response contains no choices: %s" % str(err)[:200],
                provider=self.name, model=model)
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        finish = choice.get("finish_reason") or "stop"
        explicit_refusal = message.get("refusal")
        usage_raw = data.get("usage") or {}
        details = usage_raw.get("prompt_tokens_details") or {}
        usage = {
            "input_tokens": usage_raw.get("prompt_tokens", 0),
            "output_tokens": usage_raw.get("completion_tokens", 0),
            "cached_tokens": details.get("cached_tokens", 0),
        }
        refusal = detect_refusal(content, finish, explicit_refusal)
        return self.normalize(data.get("model", model), content, usage,
                              finish, refusal)
