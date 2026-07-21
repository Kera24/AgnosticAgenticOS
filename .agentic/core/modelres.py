"""Central model-resolution policy.

The single place that decides, for one role/backend combination, what
model (if any) actually gets sent to a backend -- and why. The exact same
`resolve_model()` call is used both at real invocation time (Codex,
Claude/Qwen-style configured CLIs, Ollama, API adapters) and by
setup/doctor for readiness reporting, so "READY" and "the invocation
actually works" can never disagree again.

Policy summary:

- CLI backends (Codex, Claude Code, and any future subscription CLI):
  a missing/`auto`/placeholder configured model means "let the
  authenticated CLI choose its own default" -- no `--model` flag is ever
  emitted, and the resolved model is reported as `provider_default`. An
  explicitly configured, non-placeholder model is passed through
  unchanged. CLI model names are never assumed interchangeable with API
  model names, so only the backend's OWN configured model
  (`backends.<name>.model`) is consulted -- never a role's legacy,
  API-oriented model name.
- Local (Ollama): the configured model must be a real, installed,
  non-embedding generation model, validated against `ollama list`.
  Resolution never falls through to an arbitrary or embedding-only
  model; an unresolvable configuration is reported as a clear
  pre-invocation error, never a live HTTP failure discovered mid-call.
- API backends: an explicit, non-placeholder model is mandatory.
  A placeholder or missing model is invalid configuration and is never
  silently substituted with an arbitrary provider model.
"""
import re

# One predicate, reused by setup, doctor, routing, and every backend
# invocation -- see module docstring. Do not duplicate this list elsewhere.
PLACEHOLDER_EXACT = {
    "example/small-triage-model", "example-fallback-model",
    "example-reasoning-model", "example-coding-model",
    "example-local-model", "example-verification-model",
}
PLACEHOLDER_PREFIXES = ("example/", "example-", "configurable")
AUTO_ALIASES = {"auto"}

# Embedding models are never valid for a generative role. Kept here as the
# one canonical pattern -- routing.py imports it rather than redefining it.
EMBEDDING_RE = re.compile(r"(?i)(embed|embedding|bge-|-e5|e5-|minilm|"
                          r"nomic-embed)")


def is_placeholder_model(value):
    """True for None/empty/`auto`/known placeholders/anything beginning
    with `example/`, `example-`, or `configurable` -- the single source
    of truth for "this is not a real, usable model name"."""
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    if text.lower() in AUTO_ALIASES:
        return True
    if text in PLACEHOLDER_EXACT:
        return True
    if text.startswith(PLACEHOLDER_PREFIXES):
        return True
    return False


def is_embedding_model(value):
    return bool(value) and bool(EMBEDDING_RE.search(str(value)))


class Resolution(dict):
    """Plain-dict result so callers can use `.get(...)` or attribute-style
    documented keys interchangeably: role, backend, backend_type,
    configured_model, role_model, resolved_model, model_source,
    model_flag_emitted, valid, explanation."""


def _result(role, backend_name, backend_type, role_model, backend_model,
           resolved, emit_flag, source, valid, explanation):
    return Resolution(
        role=role, backend=backend_name, backend_type=backend_type,
        role_model=role_model, configured_model=backend_model,
        resolved_model=resolved, model_source=source,
        model_flag_emitted=bool(emit_flag), valid=bool(valid),
        explanation=explanation)


def resolve_model(role, backend_type, backend_name, *, role_model=None,
                  backend_model=None, detected_models=None,
                  backend_kind=None):
    """Resolve the model for one role/backend combination.

    role_model: cfg["roles"][role]["model"] when that legacy, API-shaped
        role config exists -- informational only; CLI and local backends
        never send it to the backend (see module docstring).
    backend_model: cfg["backends"][backend_name]["model"] -- the only
        configured value CLI and API backends act on.
    detected_models: real models `ollama list` reports installed;
        required to validate a local-backend resolution.
    """
    if backend_type == "cli":
        if is_placeholder_model(backend_model):
            return _result(
                role, backend_name, backend_type, role_model, backend_model,
                "provider_default", False, "cli_provider_default", True,
                "%s: no explicit model configured for this CLI (%r) -- the "
                "authenticated CLI selects its own subscription default; "
                "no model flag is emitted" % (backend_name, backend_model))
        return _result(
            role, backend_name, backend_type, role_model, backend_model,
            str(backend_model), True, "explicit_config", True,
            "%s: explicit model %r configured for this backend"
            % (backend_name, backend_model))

    if backend_type == "local":
        candidate, source = backend_model, "explicit_config"
        if is_placeholder_model(candidate):
            candidate, source = None, "local_default"
        installed = list(detected_models or [])
        if candidate is None:
            generation = [m for m in installed if not is_embedding_model(m)]
            if not generation:
                return _result(
                    role, backend_name, backend_type, role_model,
                    backend_model, None, False, "unresolved", False,
                    "%s: no model configured and no installed generation "
                    "model found (embedding-only models are never "
                    "auto-selected) -- run `ollama pull <model>` or set "
                    "backends.%s.model" % (backend_name, backend_name))
            candidate = generation[0]
        if is_embedding_model(candidate):
            return _result(
                role, backend_name, backend_type, role_model, backend_model,
                candidate, False, source, False,
                "%s: configured model %r is embedding-only and cannot "
                "serve a generative role" % (backend_name, candidate))
        if installed and candidate not in installed:
            return _result(
                role, backend_name, backend_type, role_model, backend_model,
                candidate, False, source, False,
                "%s: configured model %r is not among the installed "
                "models (%s) -- check for a typo or run `ollama pull %s`"
                % (backend_name, candidate, ", ".join(installed) or "none",
                   candidate))
        return _result(
            role, backend_name, backend_type, role_model, backend_model,
            candidate, True, source, True,
            "%s: using installed model %r" % (backend_name, candidate))

    # api / custom_command: an explicit, non-placeholder model is mandatory
    if is_placeholder_model(backend_model):
        return _result(
            role, backend_name, backend_type, role_model, backend_model,
            None, False, "api_required", False,
            "%s: an explicit, non-placeholder model is required for API "
            "backends -- placeholders are never silently substituted"
            % backend_name)
    return _result(
        role, backend_name, backend_type, role_model, backend_model,
        str(backend_model), True, "explicit_config", True,
        "%s: explicit model %r configured" % (backend_name, backend_model))


def diagnostic_lines(resolution):
    """Safe, credential-free diagnostic text for error reports/logs --
    never a full prompt, never credentials."""
    r = resolution
    return [
        "role=%s" % r.get("role"),
        "backend=%s" % r.get("backend"),
        "backend_mode=%s" % r.get("backend_type"),
        "configured_model=%s" % r.get("configured_model"),
        "resolved_model=%s" % r.get("resolved_model"),
        "model_source=%s" % r.get("model_source"),
        "model_flag_emitted=%s" % str(r.get("model_flag_emitted")).lower(),
    ]
