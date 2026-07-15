"""Ollama local models via its OpenAI-compatible endpoint. Keyless and
cost-free by default."""
from .openai_compatible import OpenAICompatibleProvider


class OllamaProvider(OpenAICompatibleProvider):
    DEFAULT_BASE_URL = "http://localhost:11434/v1"

    def __init__(self, name, cfg, transport=None, env=None):
        cfg = dict(cfg or {})
        cfg.setdefault("api_key_required", False)
        cfg.setdefault("cost_free", True)
        super().__init__(name, cfg, transport=transport, env=env)
