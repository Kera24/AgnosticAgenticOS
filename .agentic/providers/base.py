"""Provider base: normalized responses, capability metadata, shared HTTP
transport, and refusal detection.

Every adapter returns the same dict shape regardless of vendor:
{
  "ok": bool, "provider": str, "model": str, "content": str,
  "structured_output": dict, "usage": {"input_tokens", "output_tokens",
  "cached_tokens"}, "estimated_cost_usd": float, "finish_reason": str,
  "refusal": bool, "error": dict|None
}
An HTTP 200 is never treated as success by itself — bodies are validated and
failure modes mapped onto the typed errors in core.errors.
"""
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import errors  # noqa: E402

DEFAULT_CAPABILITIES = {
    "tool_calling": False,
    "structured_output": False,
    "usage_reporting": False,
    "refusal_reporting": False,
    "reasoning_control": False,
    "context_window": None,
}

_REFUSAL_RE = re.compile(
    r"^\s*(i('m| am) sorry[, ]|i can(no|')t (help|assist|comply)"
    r"|i (won't|will not) (help|assist|provide)"
    r"|i'?m (unable|not able) to (help|assist|comply|provide))",
    re.IGNORECASE,
)


def detect_refusal(content, finish_reason=None, explicit=None):
    if explicit:
        return True
    if finish_reason in ("content_filter", "refusal"):
        return True
    return bool(content) and bool(_REFUSAL_RE.match(content))


def default_transport(url, headers, body, timeout):
    """POST JSON; return (status_code, response_text). Maps transport-level
    failures to typed errors."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (socket.timeout, TimeoutError) as exc:
        raise errors.TimeoutError_("transport timeout: %s" % exc)
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
            raise errors.TimeoutError_("transport timeout: %s" % exc.reason)
        raise errors.ProviderError("connection failed: %s" % exc.reason)


class BaseProvider:
    capabilities = dict(DEFAULT_CAPABILITIES)

    def __init__(self, name, cfg, transport=None, env=None):
        self.name = name
        self.cfg = cfg or {}
        self.transport = transport or default_transport
        self.env = env if env is not None else os.environ
        self.cost_free = bool(self.cfg.get("cost_free", False))

    # -- credentials -------------------------------------------------------
    def api_key(self, required=True):
        key_env = self.cfg.get("api_key_env")
        required = required and self.cfg.get("api_key_required", True)
        key = self.env.get(key_env, "") if key_env else ""
        if required and not key:
            raise errors.AuthError(
                "environment variable %s is not set" % (key_env or "<api_key_env>"),
                provider=self.name)
        return key

    def base_url(self, default=None):
        default = default or getattr(self, "DEFAULT_BASE_URL", None)
        if self.cfg.get("base_url"):
            return self.cfg["base_url"].rstrip("/")
        url_env = self.cfg.get("base_url_env")
        if url_env:
            url = self.env.get(url_env, "")
            if not url:
                raise errors.ProviderError(
                    "environment variable %s (base_url_env) is not set" % url_env,
                    provider=self.name)
            return url.rstrip("/")
        if default:
            return default.rstrip("/")
        raise errors.ProviderError("no base_url configured", provider=self.name)

    # -- normalization -----------------------------------------------------
    def normalize(self, model, content, usage=None, finish_reason="stop",
                  refusal=False, structured_output=None):
        usage = usage or {}
        return {
            "ok": True,
            "provider": self.name,
            "model": model,
            "content": content or "",
            "structured_output": structured_output or {},
            "usage": {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cached_tokens": int(usage.get("cached_tokens") or 0),
            },
            "estimated_cost_usd": 0.0,   # filled in by the budget layer
            "finish_reason": finish_reason or "stop",
            "refusal": bool(refusal),
            "error": None,
        }

    def map_http_error(self, status, body, model):
        text = body[:400] if isinstance(body, str) else str(body)[:400]
        low = text.lower()
        if status in (401, 403):
            raise errors.AuthError(text, provider=self.name, model=model)
        if status == 429:
            raise errors.RateLimitError(text, provider=self.name, model=model)
        if status == 404 or "model_not_found" in low or "does not exist" in low:
            raise errors.ModelUnavailableError(text, provider=self.name, model=model)
        if status == 400 and ("context length" in low or "context_length" in low
                              or "maximum context" in low or "too many tokens" in low):
            raise errors.ContextLengthError(text, provider=self.name, model=model)
        if status >= 500:
            raise errors.ProviderError("HTTP %d: %s" % (status, text),
                                       provider=self.name, model=model)
        raise errors.ProviderError("HTTP %d: %s" % (status, text),
                                   provider=self.name, model=model)

    def parse_json_body(self, body, model):
        try:
            return json.loads(body)
        except ValueError:
            raise errors.ProviderError("non-JSON response body: %s" % body[:200],
                                       provider=self.name, model=model)

    # -- interface ---------------------------------------------------------
    def invoke(self, model, prompt, input_data=None, tools=None, timeout=120,
               max_output_tokens=None, temperature=0):
        raise NotImplementedError
