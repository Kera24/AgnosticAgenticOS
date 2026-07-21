"""Typed error taxonomy. HTTP 200 does not mean success; each failure mode
is distinguished so retry/fallback policy can act on the kind, not the text."""


class AgenticError(Exception):
    kind = "error"

    def __init__(self, detail="", provider=None, model=None):
        super().__init__(detail)
        self.detail = str(detail)
        self.provider = provider
        self.model = model
        # Optional model-resolution diagnostic (role/backend/configured
        # model/resolved model/source/flag-emitted) -- see core.modelres.
        # Never carries prompts or credentials.
        self.diagnostic = None

    def as_dict(self):
        out = {"kind": self.kind, "detail": self.detail,
               "provider": self.provider, "model": self.model}
        if self.diagnostic:
            out["diagnostic"] = self.diagnostic
        return out


class AuthError(AgenticError):
    kind = "auth"          # never triggers fallback: it is a config problem


class RateLimitError(AgenticError):
    kind = "rate_limit"    # retry, then fallback


class TimeoutError_(AgenticError):
    kind = "timeout"       # retry, then fallback


class ModelUnavailableError(AgenticError):
    kind = "model_unavailable"   # fallback


class ContextLengthError(AgenticError):
    kind = "context_length"      # fallback (a different model may fit)


class ProviderError(AgenticError):
    kind = "provider_error"      # 5xx / outage: retry, then fallback


class MalformedOutputError(AgenticError):
    kind = "malformed_output"    # one schema-repair retry, then fallback/queue


class RefusalError(AgenticError):
    kind = "refusal"             # fallback only if retry.fallback_on_refusal


class ToolExecutionError(AgenticError):
    kind = "tool_execution"


class BudgetExceededError(AgenticError):
    kind = "budget_exceeded"     # never falls back; stop safely


class PolicyError(AgenticError):
    kind = "policy"              # contract/guardrail violation; queue or stop


class UsageLimitError(AgenticError):
    kind = "usage_limit"         # subscription quota exhausted; cool + reroute

    def __init__(self, detail="", provider=None, model=None,
                 retry_after_seconds=None, reset_at=None):
        super().__init__(detail, provider, model)
        self.retry_after_seconds = retry_after_seconds
        self.reset_at = reset_at


class PermissionDeniedError(AgenticError):
    kind = "permission_denied"   # sandbox/approval denial; never bypass


class InterruptedProcessError(AgenticError):
    kind = "interrupted"         # process killed/crashed mid-run


class UnknownFailureError(AgenticError):
    kind = "unknown"


class BackendUnavailableError(AgenticError):
    kind = "backend_unavailable"  # CLI not installed / server down


# Error kinds that justify switching to the fallback role/backend.
FALLBACK_KINDS = {"rate_limit", "timeout", "model_unavailable",
                  "context_length", "provider_error", "usage_limit",
                  "interrupted", "unknown", "backend_unavailable"}
# Error kinds worth retrying on the same provider first.
RETRYABLE_KINDS = {"rate_limit", "timeout", "provider_error"}
# Kinds that must NEVER trigger fallback (config problems / safety refusals
# must not be routed around).
NO_FALLBACK_KINDS = {"auth", "policy", "budget_exceeded", "permission_denied"}
