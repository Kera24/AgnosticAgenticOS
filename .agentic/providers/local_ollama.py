"""Local Ollama backend: detection and model discovery via the `ollama` CLI,
inference via its OpenAI-compatible HTTP endpoint (reusing the existing
adapter). Monetary cost is zero; time/token limits still apply.

The configured model is validated against `ollama list` BEFORE the HTTP
call: a typo'd or uninstalled model name is reported as a clear
pre-invocation configuration error (core.modelres), not discovered as a
confusing HTTP 404 mid-call.

Timeouts are Ollama's OWN, separate from `execution.command_timeout_seconds`
(which still governs every other backend unchanged): a local model can need
substantial cold-start time to load into memory before it can generate a
single token, which a CLI/API-shaped flat timeout was never sized for."""
import shutil
import time

from core import errors
from core import execpolicy
from core.modelres import resolve_model

from .ollama import OllamaProvider

DEFAULT_OLLAMA_TIMEOUTS = {
    "connect_timeout_seconds": 10,
    "first_token_timeout_seconds": 300,
    "total_timeout_seconds": 900,
    "cold_start_grace_seconds": 300,
}


def ollama_timeout_config(backend_cfg):
    """Effective Ollama timeout policy from the Ollama backend's OWN
    config dict (`cfg["backends"]["ollama"]` -- the same dict
    `OllamaLocalBackend` already receives as `self.cfg`); never affects any
    other backend's timeout.

    `total_timeout_seconds` (+ `cold_start_grace_seconds` when the model
    isn't already warm) is the real ceiling actually enforced below, via the
    single blocking HTTP call the current transport makes. `connect_timeout_
    seconds` and `first_token_timeout_seconds` are accepted, validated, and
    returned here for configuration/documentation purposes and future use,
    but are not independently enforceable without a streaming-capable
    transport (`providers.openai_compatible` is a single non-streaming
    request/response) -- noted honestly rather than silently faked."""
    merged = dict(DEFAULT_OLLAMA_TIMEOUTS)
    for key in DEFAULT_OLLAMA_TIMEOUTS:
        if (backend_cfg or {}).get(key) is not None:
            merged[key] = backend_cfg[key]
    return merged


def loaded_models(runner=None, which=None):
    """Models Ollama currently has warm in memory (`ollama ps`) -- used only
    to distinguish a cold start from an ordinary call for diagnostics/grace-
    period purposes. Never required for correctness: an unparsable or absent
    `ollama ps` just means "assume cold", the conservative default."""
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
                 env=None):
        self.name = name
        self.cfg = cfg or {}
        self.runner = runner
        self.which = which
        self.provider = OllamaProvider(name, self.cfg, transport=transport,
                                       env=env)
        self.last_model_resolution = None

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
        timeouts = ollama_timeout_config(self.cfg)
        warm = model in loaded_models(runner=self.runner, which=self.which)

        effective_timeout = timeouts["total_timeout_seconds"]
        if not warm:
            effective_timeout += timeouts["cold_start_grace_seconds"]
        started = time.time()
        try:
            resp = self.provider.invoke(
                model, prompt, input_data=input_data,
                timeout=effective_timeout,
                max_output_tokens=self.cfg.get("max_output_tokens"),
                temperature=self.cfg.get("temperature", 0))
        except errors.TimeoutError_ as exc:
            elapsed = round(time.time() - started, 1)
            stage = "cold_start" if not warm else "generation"
            exc.detail = (
                "ollama %s: %s (model %s, elapsed %.1fs of %ss budget)"
                % (model, "still warming up" if not warm
                   else "generation timed out", model, elapsed,
                   effective_timeout))
            exc.diagnostic = (exc.diagnostic or []) + [
                "ollama_timeout_stage=%s" % stage,
                "ollama_warm_before_call=%s" % str(warm).lower(),
                "ollama_effective_timeout_seconds=%s" % effective_timeout,
                "ollama_elapsed_seconds=%s" % elapsed]
            raise
        usage = resp.get("usage") or {}
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
        }
