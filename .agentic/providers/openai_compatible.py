"""Any OpenAI-compatible endpoint: OpenRouter, Qwen (DashScope compatible
mode), vLLM, LM Studio, llama.cpp server, future providers. Configure
base_url or base_url_env; api_key_required: false for keyless servers.
Capabilities are conservative because the actual backend varies."""
from .openai import OpenAIProvider


class OpenAICompatibleProvider(OpenAIProvider):
    capabilities = {
        "tool_calling": False,
        "structured_output": False,
        "usage_reporting": True,     # usually present; treated as optional
        "refusal_reporting": False,
        "reasoning_control": False,
        "context_window": None,
    }
    DEFAULT_BASE_URL = None   # must come from config or env

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        key = self.api_key(required=self.cfg.get("api_key_required", True))
        if key:
            headers["Authorization"] = "Bearer " + key
        return headers
