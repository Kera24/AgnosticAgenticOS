"""Provider adapters. The orchestration layer only ever sees the normalized
response shape from providers.base; adding a provider means adding one module
here and referencing its type in config.yaml."""
from . import base  # noqa: F401


def build(name, pcfg, transport=None, env=None):
    """Instantiate the adapter for a configured provider entry."""
    ptype = (pcfg or {}).get("type", name)
    if ptype == "openai":
        from .openai import OpenAIProvider
        return OpenAIProvider(name, pcfg, transport=transport, env=env)
    if ptype == "anthropic":
        from .anthropic import AnthropicProvider
        return AnthropicProvider(name, pcfg, transport=transport, env=env)
    if ptype in ("openai_compatible", "openrouter"):
        from .openai_compatible import OpenAICompatibleProvider
        return OpenAICompatibleProvider(name, pcfg, transport=transport, env=env)
    if ptype == "ollama":
        from .ollama import OllamaProvider
        return OllamaProvider(name, pcfg, transport=transport, env=env)
    if ptype == "custom_command":
        from .custom_command import CustomCommandProvider
        return CustomCommandProvider(name, pcfg, env=env)
    raise KeyError("unknown provider type %r for provider %r" % (ptype, name))
