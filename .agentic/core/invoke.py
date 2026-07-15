"""The single entry point the orchestration layer uses to talk to any model.

invoke_model() handles: role -> provider/model resolution, budget checks
(before AND after every invocation, including fallbacks), retry with backoff,
role-specific fallback, refusal handling, local JSON extraction, schema
validation, and one schema-repair retry. It always returns the normalized
response dict; `ok: false` plus a typed `error` describes any failure.

Fallback policy (config `retry:`): only for rate_limit / timeout /
model_unavailable / context_length / provider_error — and refusal when
`fallback_on_refusal: true`. Auth errors, policy errors, and budget stops
never fall back; fallback is never used to bypass safety."""
import time

import providers as provider_registry

from . import errors
from .budget import Budget  # noqa: F401  (re-export for callers)
from .config import provider_config, resolve_role
from .jsonx import extract_first_json
from .schema import validate

REPAIR_SUFFIX = (
    "\n\n# SCHEMA REPAIR\nYour previous response was not valid JSON for the "
    "required schema. Violations:\n%s\n"
    "Return ONLY the corrected JSON object. No prose, no code fences.")


def _error_response(role_cfg, err):
    return {
        "ok": False,
        "provider": err.provider or role_cfg.get("provider", "?"),
        "model": err.model or role_cfg.get("model", "?"),
        "content": "",
        "structured_output": {},
        "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0},
        "estimated_cost_usd": 0.0,
        "finish_reason": "error",
        "refusal": err.kind == "refusal",
        "error": err.as_dict(),
    }


def invoke_model(cfg, role, prompt, input_data=None, output_schema=None,
                 tools=None, timeout=None, max_output_tokens=None, *,
                 budget, transport=None, log=None, env=None,
                 sleeper=time.sleep, _seen_roles=None):
    _seen_roles = set(_seen_roles or ())
    _seen_roles.add(role)
    log = log or (lambda event: None)
    retry_cfg = cfg.get("retry", {}) or {}
    attempts = max(1, int(retry_cfg.get("maximum_attempts_per_provider", 2)))
    backoffs = retry_cfg.get("backoff_seconds", [2, 10]) or [0]

    role_cfg = resolve_role(cfg, role)
    provider_name = role_cfg["provider"]
    pcfg = provider_config(cfg, provider_name)
    model = role_cfg["model"]
    temperature = role_cfg.get("temperature", 0)
    max_out = max_output_tokens or role_cfg.get("max_output_tokens")
    timeout = timeout or cfg.get("execution", {}).get("command_timeout_seconds", 900)

    def _fallback(reason_err):
        fb_role = role_cfg.get("fallback_role")
        if (not retry_cfg.get("allow_fallback", True) or not fb_role
                or fb_role in _seen_roles):
            return None
        if reason_err.kind not in errors.FALLBACK_KINDS and not (
                reason_err.kind == "refusal"
                and retry_cfg.get("fallback_on_refusal", False)) and not (
                reason_err.kind == "malformed_output"):
            return None
        log({"event": "fallback", "role": role, "from_provider": provider_name,
             "from_model": model, "to_role": fb_role,
             "reason": reason_err.kind, "detail": reason_err.detail[:200]})
        return invoke_model(cfg, fb_role, prompt, input_data, output_schema,
                            tools, timeout, max_output_tokens, budget=budget,
                            transport=transport, log=log, env=env,
                            sleeper=sleeper, _seen_roles=_seen_roles)

    # budget gate before this invocation (also runs before each fallback)
    try:
        entry, source = budget.check_before_call(provider_name, pcfg, model, role)
    except errors.BudgetExceededError as exc:
        log({"event": "budget_stop", "role": role, "detail": exc.detail})
        return _error_response(role_cfg, exc)

    try:
        provider = provider_registry.build(provider_name, pcfg,
                                           transport=transport, env=env)
    except (KeyError, errors.AgenticError) as exc:
        err = exc if isinstance(exc, errors.AgenticError) else \
            errors.ProviderError(str(exc), provider=provider_name, model=model)
        return _error_response(role_cfg, err)

    def _call(call_prompt):
        last = None
        for attempt in range(attempts):
            try:
                return provider.invoke(model, call_prompt, input_data, tools,
                                       timeout, max_out, temperature)
            except errors.AgenticError as exc:
                last = exc
                log({"event": "provider_error", "role": role, "kind": exc.kind,
                     "attempt": attempt + 1, "detail": exc.detail[:200]})
                if exc.kind in errors.RETRYABLE_KINDS and attempt < attempts - 1:
                    sleeper(backoffs[min(attempt, len(backoffs) - 1)])
                    continue
                raise
        raise last  # pragma: no cover

    try:
        response = _call(prompt)
    except errors.AgenticError as exc:
        fb = _fallback(exc)
        return fb if fb is not None else _error_response(role_cfg, exc)

    budget.settle(response, entry, source, role,
                  status="refusal" if response["refusal"] else "ok",
                  prompt_text=prompt)

    if response["refusal"]:
        err = errors.RefusalError("model refused", provider=provider_name,
                                  model=model)
        fb = _fallback(err)
        if fb is not None:
            return fb
        response["ok"] = False
        response["error"] = err.as_dict()
        return response

    if output_schema is None:
        return response

    # structured output: extract + validate locally, repair once, then fallback
    structured = extract_first_json(response["content"])
    violations = (validate(structured, output_schema) if structured is not None
                  else ["no JSON object found in response"])
    if not violations:
        response["structured_output"] = structured
        return response

    log({"event": "malformed_output", "role": role,
         "violations": violations[:5]})
    try:
        budget.check_before_call(provider_name, pcfg, model, role)
        repair = _call(prompt + REPAIR_SUFFIX % "\n".join(violations[:10]))
        budget.settle(repair, entry, source, role, status="repair",
                      prompt_text=prompt)
        structured = extract_first_json(repair["content"])
        violations = (validate(structured, output_schema)
                      if structured is not None
                      else ["no JSON object found in repaired response"])
        if not violations:
            repair["structured_output"] = structured
            return repair
        response = repair
    except errors.AgenticError:
        pass

    err = errors.MalformedOutputError("; ".join(violations[:5]),
                                      provider=provider_name, model=model)
    fb = _fallback(err)
    if fb is not None:
        return fb
    response["ok"] = False
    response["error"] = err.as_dict()
    return response
