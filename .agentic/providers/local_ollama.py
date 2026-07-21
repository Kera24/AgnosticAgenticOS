"""Local Ollama backend: detection and model discovery via the `ollama` CLI,
inference via its OpenAI-compatible HTTP endpoint (reusing the existing
adapter). Monetary cost is zero; time/token limits still apply.

The configured model is validated against `ollama list` BEFORE the HTTP
call: a typo'd or uninstalled model name is reported as a clear
pre-invocation configuration error (core.modelres), not discovered as a
confusing HTTP 404 mid-call."""
import shutil

from core import errors
from core import execpolicy
from core.modelres import resolve_model

from .ollama import OllamaProvider


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
        resp = self.provider.invoke(
            model, prompt, input_data=input_data, timeout=timeout,
            max_output_tokens=self.cfg.get("max_output_tokens"),
            temperature=self.cfg.get("temperature", 0))
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
