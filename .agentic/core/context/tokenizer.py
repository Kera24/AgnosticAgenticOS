"""Tokenizer abstraction.

Provider tokenizers are used when a backend exposes one (none of the
built-in backends currently do, and no network call is ever made to count
tokens). Otherwise a documented conservative estimate applies:

    tokens ≈ ceil(characters / 4) * safety_multiplier

chars/4 is the common English-code average; the safety multiplier (default
1.20, configurable as context.safety_multiplier) biases the estimate high so
a package that fits on paper fits in practice. Estimates are always labelled
estimated — never presented as provider-reported counts.
"""
import math

DEFAULT_SAFETY_MULTIPLIER = 1.20

# registry of provider tokenizers: name -> callable(text) -> int
_PROVIDER_TOKENIZERS = {}


def register_tokenizer(name, fn):
    """Register an exact tokenizer for a backend/model family."""
    _PROVIDER_TOKENIZERS[name] = fn


def estimate_tokens(text, provider=None, safety_multiplier=None):
    """Conservative token estimate for `text`. Uses a registered provider
    tokenizer when available (exact, multiplier not applied), else the
    documented chars/4 heuristic biased by the safety multiplier."""
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    exact = _PROVIDER_TOKENIZERS.get(provider)
    if exact is not None:
        try:
            return int(exact(text))
        except Exception:
            pass  # fall through to the heuristic; never fail a build on this
    multiplier = DEFAULT_SAFETY_MULTIPLIER if safety_multiplier is None \
        else max(1.0, float(safety_multiplier))
    return int(math.ceil(len(text) / 4.0 * multiplier))
