"""Local Ollama backend: detection and model discovery via the `ollama` CLI,
inference via its native streaming `/api/chat` endpoint (see providers.ollama
-- no longer the OpenAI-compatible shim, which couldn't stream, couldn't
report Ollama's own telemetry, and couldn't separate "thinking" from final
content). Monetary cost is zero; time/token limits still apply.

The configured model is validated against `ollama list` BEFORE the HTTP
call: a typo'd or uninstalled model name is reported as a clear
pre-invocation configuration error (core.modelres), not discovered as a
confusing HTTP 404 mid-call.

Timeouts are Ollama's OWN, separate from `execution.command_timeout_seconds`
(which still governs every other backend unchanged), with five independent
stages (connect / model-load / first-token / idle-stream / total) enforced
by the native streaming transport itself (see providers.ollama) -- a local
model can need substantial cold-start time to load into memory before it
can generate a single token, which a CLI/API-shaped flat timeout was never
sized for, and the stream is now genuinely observed rather than guessed at
via an `ollama ps` pre-check.

Context sizing is dynamic: `num_ctx` is chosen from the ACTUAL Context
Package the caller composed (estimated from the rendered prompt this
backend already receives, not a fixed number), rounded up to the smallest
allowed tier, capped by the machine-capacity ceiling this admin configured
(`max_context_tokens`) -- never the maximum tier just because it exists."""
import json
import shutil
import time

from core import errors
from core import execpolicy
from core.capacity import estimate_tokens_from_text
from core.modelres import resolve_model

from .base import default_transport
from .ollama import OllamaProvider

DEFAULT_OLLAMA_TIMEOUTS = {
    "connect_timeout_seconds": 10,
    "model_load_timeout_seconds": 600,
    "first_token_timeout_seconds": 300,
    "idle_stream_timeout_seconds": 180,
    "total_timeout_seconds": 1800,
}

DEFAULT_OLLAMA_OPTIONS = {"num_ctx": 32768, "num_predict": 4096,
                          "temperature": 0.2}

CONTEXT_TIERS = (8192, 16384, 32768, 65536)
DEFAULT_MAX_CONTEXT_TOKENS = 32768
DEFAULT_CONTEXT_SAFETY_MARGIN_TOKENS = 1024

DEFAULT_KEEP_ALIVE_MINUTES = 30

# Per the spec: reasoning-heavy orchestrator roles get light thinking,
# workers execute directly, reviewers get light thinking too -- an
# unrecognised role degrades to "no thinking requested" (None), never a
# guessed value.
DEFAULT_THINKING_BY_ROLE = {"architect": "low", "worker": False,
                           "reviewer": "low"}


def ollama_timeout_config(backend_cfg):
    """Effective Ollama timeout policy from the Ollama backend's OWN
    config dict (`cfg["backends"]["ollama"]` -- the same dict
    `OllamaLocalBackend` already receives as `self.cfg`); never affects any
    other backend's timeout."""
    merged = dict(DEFAULT_OLLAMA_TIMEOUTS)
    for key in DEFAULT_OLLAMA_TIMEOUTS:
        if (backend_cfg or {}).get(key) is not None:
            merged[key] = backend_cfg[key]
    return merged


def ollama_options_for_role(backend_cfg, role):
    """Global `backends.ollama.options` merged with a per-role override
    (`backends.ollama.options_by_role.<role>`) -- the role wins. `num_ctx`
    set here is only a starting point; `invoke()` below replaces it with
    the dynamically-selected tier."""
    merged = dict(DEFAULT_OLLAMA_OPTIONS)
    merged.update((backend_cfg or {}).get("options") or {})
    merged.update(((backend_cfg or {}).get("options_by_role") or {})
                 .get(role) or {})
    return merged


def select_context_tier(required_tokens, *, max_context_tokens,
                        tiers=CONTEXT_TIERS):
    """Round UP to the smallest allowed tier that covers `required_tokens`,
    capped at `max_context_tokens` (the admin-configured machine-capacity
    ceiling -- this platform never auto-detects RAM; the ceiling itself IS
    the capacity check, exactly like every other self-imposed limit in
    this codebase, never invented). Never allocates the largest tier just
    because it exists."""
    allowed = sorted(t for t in tiers if t <= max_context_tokens)
    if not allowed:
        return max_context_tokens
    for tier in allowed:
        if required_tokens <= tier:
            return tier
    return allowed[-1]


def estimate_required_context(prompt, *, output_reserve, safety_margin):
    """required_context = input_estimate + output_reserve + safety_margin.
    `input_estimate` comes from the ACTUAL rendered Context Package text
    this backend receives (the same estimator `core.capacity` already uses
    for capacity/usage accounting) -- never a fixed guess."""
    input_estimate = estimate_tokens_from_text(prompt)
    return input_estimate + int(output_reserve) + int(safety_margin), \
        input_estimate


def thinking_for_role(backend_cfg, role):
    table = (backend_cfg or {}).get("thinking")
    if table is None:
        table = DEFAULT_THINKING_BY_ROLE
    return table.get(role, DEFAULT_THINKING_BY_ROLE.get(role))


def keep_alive_string(backend_cfg):
    minutes = (backend_cfg or {}).get("keep_alive_minutes",
                                      DEFAULT_KEEP_ALIVE_MINUTES)
    return "%dm" % int(minutes)


def loaded_models(runner=None, which=None):
    """Models Ollama currently has warm in memory (`ollama ps`) -- a
    diagnostic/dashboard status read only (Phase: dashboard warming state);
    NOT used for timeout budgeting, which the native stream now observes
    directly (see providers.ollama). An unparsable or absent `ollama ps`
    just means "unknown", never guessed."""
    which = which or shutil.which
    runner = runner or (lambda argv, timeout=30: execpolicy.run_command(
        argv, cwd=".", timeout=timeout, source="config"))
    if not which("ollama"):
        return []
    try:
        run = runner(["ollama", "ps"], timeout=10)
    except Exception:   # noqa: BLE001 -- diagnostic-only, never fatal
        return []
    if run.get("exit_code") != 0:
        return []
    models = []
    for line in (run.get("stdout") or "").splitlines()[1:]:
        parts = line.split()
        if parts:
            models.append(parts[0])
    return models


def detect_ollama(runner=None, which=None):
    """Return {installed, version, models: [...]} using supported commands
    only. Never guesses."""
    which = which or shutil.which
    runner = runner or (lambda argv, timeout=30: execpolicy.run_command(
        argv, cwd=".", timeout=timeout, source="config"))
    if not which("ollama"):
        return {"installed": False, "version": None, "models": []}
    version_run = runner(["ollama", "--version"], timeout=30)
    version = (version_run["stdout"] or "").strip()[:80] or None
    list_run = runner(["ollama", "list"], timeout=30)
    models = []
    if list_run["exit_code"] == 0:
        for line in (list_run["stdout"] or "").splitlines()[1:]:
            parts = line.split()
            if parts:
                models.append(parts[0])
    return {"installed": True, "version": version, "models": models}


class OllamaLocalBackend:
    """Adapter exposing Ollama through the common backend interface."""
    backend_type = "local"
    cost_free = True

    def __init__(self, name, cfg, transport=None, runner=None, which=None,
                env=None, unload_transport=None):
        self.name = name
        self.cfg = cfg or {}
        self.runner = runner
        self.which = which
        self.provider = OllamaProvider(name, self.cfg, transport=transport,
                                       env=env)
        # Separate injection point: unload() is a plain one-shot POST, not
        # the streaming transport `self.provider` uses -- tests fake it
        # independently so unload behaviour is verifiable without a socket.
        self.unload_transport = unload_transport or default_transport
        self.last_model_resolution = None
        self.last_telemetry = None

    def detect(self):
        return detect_ollama(runner=self.runner, which=self.which)

    def auth_status(self):
        return "ok"   # local runtime: no authentication

    def smoke_test(self, workspace):
        return self.detect()["installed"]

    def invoke(self, role, prompt, input_data, workspace, permissions,
               timeout):
        detected = self.detect().get("models") or []
        resolution = resolve_model(role, "local", self.name,
                                   backend_model=self.cfg.get("model"),
                                   detected_models=detected)
        self.last_model_resolution = resolution
        if not resolution["valid"]:
            raise errors.ModelUnavailableError(resolution["explanation"],
                                               provider=self.name)
        model = resolution["resolved_model"]

        options = ollama_options_for_role(self.cfg, role)
        max_context = int(self.cfg.get("max_context_tokens",
                                       DEFAULT_MAX_CONTEXT_TOKENS))
        safety_margin = int(self.cfg.get("context_safety_margin_tokens",
                                         DEFAULT_CONTEXT_SAFETY_MARGIN_TOKENS))
        tiers = self.cfg.get("context_tiers", CONTEXT_TIERS)
        required, input_estimate = estimate_required_context(
            prompt, output_reserve=options.get("num_predict", 0),
            safety_margin=safety_margin)
        options["num_ctx"] = select_context_tier(
            required, max_context_tokens=max_context, tiers=tiers)

        keep_alive = keep_alive_string(self.cfg)
        thinking = thinking_for_role(self.cfg, role)
        timeouts = ollama_timeout_config(self.cfg)

        started = time.time()
        resp = self.provider.invoke(
            model, prompt, input_data=input_data, options=options,
            keep_alive=keep_alive, thinking=thinking, timeouts=timeouts)
        elapsed = round(time.time() - started, 1)

        usage = resp.get("usage") or {}
        telemetry = dict(resp.get("telemetry") or {})
        telemetry.update(elapsed_seconds=elapsed,
                         input_estimate_tokens=input_estimate,
                         required_context_tokens=required)
        self.last_telemetry = telemetry
        return {
            "ok": True, "backend": self.name, "backend_type": "local",
            "model": resp.get("model", model), "role": role,
            "provider": self.name,
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
            "exit_code": 0, "estimated_cost_usd": 0.0, "error": None,
            "telemetry": telemetry,
        }

    def unload(self, reason="user_requested"):
        """Force Ollama to release the configured model from memory now
        (`keep_alive: 0`) -- called when a project completes, a user
        explicitly requests it (`models unload <backend>`), or a future
        resource manager needs the memory for another local model.
        Best-effort: never raises -- an already-unloaded model, or Ollama
        not running at all, is reported, not fatal."""
        model = self.cfg.get("model")
        url = self.provider.base_url(self.provider.DEFAULT_BASE_URL) + \
            "/api/chat"
        body = json.dumps({"model": model, "keep_alive": 0,
                          "messages": []}).encode("utf-8")
        try:
            status, text = self.unload_transport(
                url, {"Content-Type": "application/json"}, body, 10)
            ok = status == 200
            return {"ok": ok, "backend": self.name, "model": model,
                   "reason": reason,
                   "detail": None if ok else text[:200]}
        except Exception as exc:   # noqa: BLE001 -- unload is always best-effort
            return {"ok": False, "backend": self.name, "model": model,
                   "reason": reason, "detail": str(exc)[:200]}
