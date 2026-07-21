"""Common backend interface. The orchestration layer calls
`invoke_backend()` and nothing else — no provider-specific logic lives above
this module.

Backend types: cli (Codex / Claude Code / Qwen / future), local (Ollama),
api (all pre-existing providers), custom_command.

Routing walks an ordered chain (primary + fallbacks). The circuit-breaker
board and self-imposed limits are consulted before every call; results feed
the capacity ledger. Fallback NEVER fires for authentication failures,
policy/permission denials, budget stops, or refusals — a safety refusal is
never routed around. Every routing decision is logged.
"""
import time

import providers as provider_registry

from . import errors
from .capacity import estimate_tokens_from_text
from .jsonx import extract_first_json
from .schema import validate

REPAIR_SUFFIX = (
    "\n\n# SCHEMA REPAIR\nYour previous response was not valid JSON for the "
    "required schema. Violations:\n%s\n"
    "Return ONLY the corrected JSON object. No prose, no code fences.")


def backend_config(cfg, name):
    backends = cfg.get("backends") or {}
    if name not in backends:
        raise KeyError("backend %r is not configured" % name)
    return backends[name]


def build_backend(cfg, name, runner=None, transport=None, which=None,
                  env=None):
    bcfg = backend_config(cfg, name)
    btype = bcfg.get("type", "api")
    if btype == "cli":
        if bcfg.get("kind", name) == "codex" or name == "codex":
            from providers.cli_codex import CodexCLIBackend
            return CodexCLIBackend(name, bcfg, runner=runner, which=which)
        from providers.cli_configured import ConfiguredCLIBackend
        return ConfiguredCLIBackend(name, bcfg, runner=runner, which=which)
    if btype == "local":
        from providers.local_ollama import OllamaLocalBackend
        return OllamaLocalBackend(name, bcfg, transport=transport,
                                  runner=runner, which=which, env=env)
    if btype in ("api", "custom_command"):
        return APIBackendAdapter(cfg, name, bcfg, transport=transport, env=env)
    raise KeyError("unknown backend type %r for backend %r" % (btype, name))


class APIBackendAdapter:
    """Wraps the existing API provider adapters (OpenAI, Anthropic,
    OpenAI-compatible, OpenRouter, Ollama-HTTP, custom_command) behind the
    common backend interface. All pre-existing providers keep working."""
    backend_type = "api"

    def __init__(self, cfg, name, bcfg, transport=None, env=None):
        self.name = name
        self.cfg = cfg
        self.bcfg = bcfg
        provider_name = bcfg.get("provider", name)
        pcfg = (cfg.get("providers") or {}).get(provider_name)
        if pcfg is None:
            raise KeyError("backend %r references unknown provider %r"
                           % (name, provider_name))
        if bcfg.get("type") == "custom_command":
            self.backend_type = "custom_command"
        self.provider = provider_registry.build(provider_name, pcfg,
                                                transport=transport, env=env)
        self.cost_free = getattr(self.provider, "cost_free", False)
        self.last_model_resolution = None

    def detect(self):
        return {"installed": True, "version": None, "models": []}

    def auth_status(self):
        try:
            self.provider.api_key(required=True)
            return "ok"
        except errors.AuthError:
            return "required"
        except Exception:
            return "unknown"

    def smoke_test(self, workspace):
        return True   # API smoke tests are opt-in live tests only

    def invoke(self, role, prompt, input_data, workspace, permissions,
               timeout):
        from .modelres import resolve_model
        resolution = resolve_model(role, self.backend_type, self.name,
                                   backend_model=self.bcfg.get("model"))
        self.last_model_resolution = resolution
        if not resolution["valid"]:
            raise errors.ModelUnavailableError(resolution["explanation"],
                                               provider=self.name)
        resp = self.provider.invoke(
            self.bcfg.get("model"), prompt, input_data=input_data,
            timeout=timeout,
            max_output_tokens=self.bcfg.get("max_output_tokens"),
            temperature=self.bcfg.get("temperature", 0))
        usage = resp.get("usage") or {}
        return {
            "ok": True, "backend": self.name,
            "backend_type": self.backend_type,
            "model": resp.get("model"), "role": role, "provider": self.name,
            "content": resp.get("content", ""), "structured_output": {},
            "usage": {"input_tokens": usage.get("input_tokens"),
                      "cached_input_tokens": usage.get("cached_tokens"),
                      "output_tokens": usage.get("output_tokens"),
                      "reasoning_tokens": None,
                      "estimated": not any(usage.values())},
            "capacity": {"remaining_reported": None, "reset_at": None,
                         "retry_after_seconds": None},
            "finish_reason": resp.get("finish_reason", "completed"),
            "refusal": resp.get("refusal", False),
            "exit_code": 0,
            "estimated_cost_usd": resp.get("estimated_cost_usd", 0.0),
            "error": None,
        }


def error_result(backend, role, err, backend_type="?"):
    return {"ok": False, "backend": backend, "backend_type": backend_type,
            "model": getattr(err, "model", None), "role": role,
            "provider": backend, "content": "", "structured_output": {},
            "usage": {"input_tokens": None, "cached_input_tokens": None,
                      "output_tokens": None, "reasoning_tokens": None,
                      "estimated": True},
            "capacity": {"remaining_reported": None,
                         "reset_at": getattr(err, "reset_at", None),
                         "retry_after_seconds": getattr(err,
                                                        "retry_after_seconds",
                                                        None)},
            "finish_reason": "error", "refusal": err.kind == "refusal",
            "exit_code": None, "estimated_cost_usd": 0.0,
            "error": err.as_dict()}


def routing_chain(cfg, role, overrides=None, memory_dir=None, board=None,
                  ledger=None, worker_chain=None):
    """Ordered backend chain for a role: CLI overrides > capability routing
    (when routing.mode: capability) > per-agent routing > simple routing."""
    overrides = overrides or {}
    if overrides.get("primary"):
        return [overrides["primary"]] + list(overrides.get("fallbacks") or [])
    routing = cfg.get("routing") or {}
    if routing.get("mode") == "capability":
        from .routing import capability_chain
        chain = capability_chain(cfg, role, memory_dir=memory_dir,
                                 board=board, ledger=ledger,
                                 worker_chain=worker_chain)
        if not chain:
            raise errors.PolicyError(
                "capability routing found no usable backend for role %r"
                % role)
        return chain
    if routing.get("mode") == "per_agent":
        per = (routing.get("per_agent") or {}).get(role)
        if per and per.get("primary"):
            return [per["primary"]] + list(per.get("fallbacks") or [])
    primary = routing.get("primary")
    if not primary:
        raise errors.PolicyError("routing.primary is not configured (run setup)")
    return [primary] + list(routing.get("fallbacks") or [])


def _fill_usage(result, prompt):
    usage = result.get("usage") or {}
    if not usage.get("input_tokens") and not usage.get("output_tokens"):
        usage = {"input_tokens": estimate_tokens_from_text(prompt),
                 "cached_input_tokens": 0,
                 "output_tokens": estimate_tokens_from_text(
                     result.get("content", "")),
                 "reasoning_tokens": None, "estimated": True}
        result["usage"] = usage
    return usage


def _attempt(backend, role, *, eligible, attempted, result, reason):
    """One entry of the routing_attempts diagnostic: every backend in the
    chain gets exactly one, whether or not it was ever actually invoked --
    the final result must never report only the last fallback's failure."""
    return {"backend": backend, "role": role, "eligible": bool(eligible),
           "attempted": bool(attempted), "result": result, "reason": reason}


def invoke_backend(cfg, backend_name, agent_role, prompt, input_data=None,
                   output_schema=None, workspace=None, permissions="read",
                   timeout=None, run_context=None, *, ledger, board,
                   fallback_chain=None, runner=None, transport=None,
                   which=None, env=None, log=None, prompt_builder=None):
    """Invoke a role on a backend chain. Returns the normalized result
    (`ok: false` + typed error rather than raising, except for programming
    errors). Every return path carries `routing_attempts`: one entry per
    backend in the chain, so a caller never sees only the final fallback's
    failure with no record of why an earlier backend was skipped.

    prompt_builder: optional callable(backend_name) -> prompt. When given,
    the prompt is REBUILT for each backend in the chain so a fallback model
    with a smaller context window receives a package built for its own
    budget — never the primary's oversized prompt."""
    log = log or (lambda event: None)
    timeout = timeout or int((cfg.get("execution") or {})
                             .get("command_timeout_seconds", 900))
    chain = [backend_name] + [b for b in (fallback_chain or [])
                              if b != backend_name]
    last_err = errors.BackendUnavailableError("no backend attempted")
    attempts = []
    for position, name in enumerate(chain):
        state = board.state(name)
        if state == "cooling":
            try:
                adapter_probe = build_backend(cfg, name, runner=runner,
                                              transport=transport,
                                              which=which, env=env)
                healthy = adapter_probe.auth_status() != "required" and \
                    adapter_probe.detect().get("installed", True)
            except Exception:
                healthy = False
            if healthy:
                board.mark_health_ok(name)
            else:
                log({"event": "routing_skip", "backend": name,
                     "reason": "health check failed while cooling"})
                attempts.append(_attempt(
                    name, agent_role, eligible=False, attempted=False,
                    result="skipped",
                    reason="circuit breaker cooling; health check failed"))
                continue
        elif state not in ("available", "degraded"):
            log({"event": "routing_skip", "backend": name, "reason": state,
                 "until": board.unavailable_until(name)})
            attempts.append(_attempt(
                name, agent_role, eligible=False, attempted=False,
                result="skipped",
                reason="circuit breaker state=%s%s" % (
                    state, (" until %s" % board.unavailable_until(name))
                    if board.unavailable_until(name) else "")))
            continue
        limit_reasons = ledger.limit_status(name)
        if limit_reasons:
            log({"event": "routing_skip", "backend": name,
                 "reason": "self-imposed limit: " + "; ".join(limit_reasons)})
            last_err = errors.UsageLimitError("; ".join(limit_reasons),
                                              provider=name)
            attempts.append(_attempt(
                name, agent_role, eligible=False, attempted=False,
                result="skipped",
                reason="self-imposed limit: " + "; ".join(limit_reasons)))
            continue
        if position > 0:
            log({"event": "fallback", "role": agent_role, "to": name,
                 "from": chain[0], "reason": last_err.kind,
                 "detail": last_err.detail[:200]})
        try:
            adapter = build_backend(cfg, name, runner=runner,
                                    transport=transport, which=which, env=env)
        except (KeyError, errors.AgenticError) as exc:
            last_err = exc if isinstance(exc, errors.AgenticError) else \
                errors.BackendUnavailableError(str(exc), provider=name)
            log({"event": "backend_build_failed", "backend": name,
                 "detail": str(exc)[:200]})
            attempts.append(_attempt(
                name, agent_role, eligible=True, attempted=False,
                result="failure",
                reason="backend build failed: %s" % str(exc)[:200]))
            continue

        if prompt_builder is not None and position > 0:
            try:
                prompt = prompt_builder(name)
            except Exception as exc:   # broker refused: budget too small
                last_err = errors.ContextLengthError(
                    "context rebuild for %s failed: %s" % (name,
                                                           str(exc)[:150]),
                    provider=name)
                log({"event": "context_rebuild_failed", "backend": name,
                     "detail": str(exc)[:200]})
                attempts.append(_attempt(
                    name, agent_role, eligible=True, attempted=False,
                    result="failure",
                    reason="context rebuild failed: %s" % str(exc)[:200]))
                continue
        started = time.time()
        try:
            result = adapter.invoke(agent_role, prompt, input_data,
                                    workspace, permissions, timeout)
        except errors.AgenticError as exc:
            duration = round(time.time() - started, 1)
            resolution = getattr(adapter, "last_model_resolution", None)
            if resolution is not None and exc.diagnostic is None:
                from .modelres import diagnostic_lines
                exc.diagnostic = diagnostic_lines(resolution)
            ledger.record_call(name, agent_role, ok=False, event=exc.kind,
                               duration_seconds=duration)
            board.record_failure(
                name, exc.kind,
                retry_after_seconds=getattr(exc, "retry_after_seconds", None),
                reset_at=getattr(exc, "reset_at", None))
            log({"event": "backend_error", "backend": name, "role": agent_role,
                 "kind": exc.kind, "detail": exc.detail[:200],
                 "diagnostic": exc.diagnostic})
            last_err = exc
            attempts.append(_attempt(
                name, agent_role, eligible=True, attempted=True,
                result="no_fallback" if exc.kind in errors.NO_FALLBACK_KINDS
                else "failure",
                reason="%s: %s" % (exc.kind, exc.detail[:200])))
            if exc.kind in errors.NO_FALLBACK_KINDS:
                failed = error_result(name, agent_role, exc,
                                      getattr(adapter, "backend_type", "?"))
                failed["routing_attempts"] = attempts
                return failed
            continue

        duration = round(time.time() - started, 1)
        board.record_success(name)
        usage = _fill_usage(result, prompt)
        ledger.record_call(name, agent_role, ok=True, usage=usage,
                           duration_seconds=duration)

        if result.get("refusal"):
            # A refusal is a safety decision; fallback never bypasses it.
            err = errors.RefusalError("backend refused", provider=name)
            log({"event": "refusal", "backend": name, "role": agent_role})
            failed = error_result(name, agent_role, err,
                                  getattr(adapter, "backend_type", "?"))
            failed["content"] = result.get("content", "")
            failed["refusal"] = True
            attempts.append(_attempt(
                name, agent_role, eligible=True, attempted=True,
                result="refused", reason="backend refused"))
            failed["routing_attempts"] = attempts
            return failed

        if output_schema is None:
            attempts.append(_attempt(
                name, agent_role, eligible=True, attempted=True,
                result="success", reason="invoked successfully"))
            result["routing_attempts"] = attempts
            return result

        structured = extract_first_json(result.get("content", ""))
        violations = (validate(structured, output_schema)
                      if structured is not None
                      else ["no JSON object found in response"])
        if not violations:
            result["structured_output"] = structured
            attempts.append(_attempt(
                name, agent_role, eligible=True, attempted=True,
                result="success", reason="invoked successfully"))
            result["routing_attempts"] = attempts
            return result
        log({"event": "malformed_output", "backend": name,
             "role": agent_role, "violations": violations[:5]})
        try:
            repair = adapter.invoke(
                agent_role, prompt + REPAIR_SUFFIX % "\n".join(violations[:10]),
                input_data, workspace, permissions, timeout)
            ledger.record_call(name, agent_role, ok=True,
                               usage=_fill_usage(repair, prompt),
                               event="repair")
            structured = extract_first_json(repair.get("content", ""))
            violations = (validate(structured, output_schema)
                          if structured is not None
                          else ["no JSON object found in repaired response"])
            if not violations:
                repair["structured_output"] = structured
                attempts.append(_attempt(
                    name, agent_role, eligible=True, attempted=True,
                    result="success",
                    reason="invoked successfully after schema repair"))
                repair["routing_attempts"] = attempts
                return repair
        except errors.AgenticError as exc:
            last_err = exc
            attempts.append(_attempt(
                name, agent_role, eligible=True, attempted=True,
                result="failure",
                reason="schema repair failed: %s: %s"
                % (exc.kind, exc.detail[:200])))
            continue
        last_err = errors.MalformedOutputError("; ".join(violations[:5]),
                                               provider=name)
        attempts.append(_attempt(
            name, agent_role, eligible=True, attempted=True,
            result="failure",
            reason="malformed output: " + "; ".join(violations[:5])))
        continue

    failed = error_result(chain[-1] if chain else backend_name, agent_role,
                          last_err)
    failed["routing_attempts"] = attempts
    log({"event": "routing_exhausted", "role": agent_role,
         "attempts": attempts})
    return failed
