"""Native Ollama API client (`/api/chat`, streamed) -- replaces the earlier
OpenAI-compatible-endpoint wrapper. Streaming lets each timeout stage
(connect / model-load / first-token / idle-stream / total) be enforced
independently instead of one flat socket timeout, and exposes Ollama's own
telemetry (load_duration, prompt_eval_count, ...) and "thinking" content
separately from final assistant content -- none of which the OpenAI-
compatible shim surfaces.

The streaming transport itself lives in `providers.base` alongside
`default_transport` -- the platform's one network choke point never
special-cases a second file for it -- and is the single injectable seam
(constructor `transport=`), exactly like every other provider's
`transport=`: automated tests always supply a fake generator, never touch
a socket."""
import json

from core import errors

from .base import BaseProvider, default_stream_transport, detect_refusal

DEFAULT_CAPABILITIES = {
    "tool_calling": False,
    "structured_output": False,
    "usage_reporting": True,
    "refusal_reporting": False,
    "reasoning_control": True,   # "think" (per-role, see local_ollama.py)
    "context_window": None,
}


class OllamaProvider(BaseProvider):
    """Native `/api/chat` client. `invoke()`'s extra keyword arguments
    (`options`, `keep_alive`, `thinking`, `timeouts`) are all optional so
    this still satisfies the common `provider.invoke(model, prompt, ...)`
    shape every other adapter uses; `providers.local_ollama` is the only
    caller that supplies them."""
    capabilities = dict(DEFAULT_CAPABILITIES)
    DEFAULT_BASE_URL = "http://localhost:11434"

    def __init__(self, name, cfg, transport=None, env=None):
        cfg = dict(cfg or {})
        cfg.setdefault("api_key_required", False)
        cfg.setdefault("cost_free", True)
        super().__init__(name, cfg, transport=transport, env=env)
        # Shape differs from BaseProvider's request/response default (a
        # generator of events, not a (status, text) tuple) -- override.
        self.transport = transport or default_stream_transport
        # Optimistic by default; degrades to boolean the first time the
        # server actually rejects a string-leveled `think` value (see
        # invoke()) -- never guessed in advance, no live capability probe.
        self.thinking_string_levels_supported = True

    def _messages(self, prompt, input_data):
        from core.context.broker import split_cache_boundary
        prefix, dynamic = split_cache_boundary(prompt)
        if dynamic is not None:
            user = dynamic
            if input_data is not None:
                if not isinstance(input_data, str):
                    input_data = json.dumps(input_data, ensure_ascii=False,
                                            indent=2)
                user = dynamic + "\n\n" + input_data
            return [{"role": "system", "content": prefix},
                   {"role": "user", "content": user}]
        messages = [{"role": "system", "content": prompt}]
        if input_data is not None:
            if not isinstance(input_data, str):
                input_data = json.dumps(input_data, ensure_ascii=False,
                                        indent=2)
            messages.append({"role": "user", "content": input_data})
        else:
            messages = [{"role": "user", "content": prompt}]
        return messages

    def invoke(self, model, prompt, input_data=None, tools=None, timeout=None,
              max_output_tokens=None, temperature=0, *, options=None,
              keep_alive=None, thinking=None, timeouts=None):
        url = self.base_url(self.DEFAULT_BASE_URL) + "/api/chat"
        think_value = thinking
        if think_value is not None and not self.thinking_string_levels_supported:
            think_value = bool(think_value)
        payload = {"model": model, "messages": self._messages(prompt, input_data),
                  "stream": True, "options": dict(options or {})}
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        if think_value is not None:
            payload["think"] = think_value
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        content_parts, thinking_parts, final = [], [], {}
        try:
            for event in self.transport(url, headers, body, timeouts or {}):
                message = event.get("message") or {}
                if message.get("thinking"):
                    thinking_parts.append(message["thinking"])
                if message.get("content"):
                    content_parts.append(message["content"])
                if event.get("done"):
                    final = event
        except errors.ProviderError as exc:
            if (think_value is not None and self.thinking_string_levels_supported
                    and "think" in str(exc).lower()):
                # the installed model/API rejected the requested thinking
                # shape (e.g. a string level on a version that only
                # supports booleans) -- degrade once, never guessed upfront
                self.thinking_string_levels_supported = False
                return self.invoke(model, prompt, input_data=input_data,
                                   timeout=timeout,
                                   max_output_tokens=max_output_tokens,
                                   temperature=temperature, options=options,
                                   keep_alive=keep_alive,
                                   thinking=bool(thinking), timeouts=timeouts)
            raise

        content = "".join(content_parts)
        usage = {"input_tokens": final.get("prompt_eval_count") or 0,
                "output_tokens": final.get("eval_count") or 0,
                "cached_tokens": 0}
        refusal = detect_refusal(content)
        result = self.normalize(final.get("model", model), content, usage,
                                "stop", refusal)
        result["thinking"] = "".join(thinking_parts)
        result["telemetry"] = {
            "load_duration": final.get("load_duration"),
            "prompt_eval_count": final.get("prompt_eval_count"),
            "prompt_eval_duration": final.get("prompt_eval_duration"),
            "eval_count": final.get("eval_count"),
            "eval_duration": final.get("eval_duration"),
            "total_duration": final.get("total_duration"),
            "selected_num_ctx": payload["options"].get("num_ctx"),
            "selected_num_predict": payload["options"].get("num_predict"),
            "thinking_mode": think_value,
            "keep_alive": keep_alive,
        }
        return result
