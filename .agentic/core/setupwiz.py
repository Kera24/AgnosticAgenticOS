"""Interactive setup: detects installed CLI/local/API backends, asks routing
and scheduling preferences, runs safe smoke tests, and writes
.agentic/config.machine.yaml (git-ignored; never stores credentials).

Fully injectable for tests: ask/echo/detectors can all be substituted, and
`answers` provides scripted input for non-interactive runs.
"""
import os

import yaml

from . import backends as backends_mod
from . import config as config_mod
from .modelres import is_embedding_model

# "model: auto" is model-NEUTRAL CLI configuration: no explicit model is
# configured, so the authenticated subscription CLI selects its own
# default (core.modelres.resolve_model). This is intentionally written for
# Codex/Claude -- setup never asks a CLI user to pick an API-style model
# name. Qwen is left unset (its readiness is authentication-gated, not
# model-gated -- see core/authx.py).
KNOWN_CLIS = {
    "codex": {"type": "cli", "kind": "codex", "binary": "codex",
              "auth_probe_args": ["login", "status"], "model": "auto"},
    "claude": {"type": "cli", "kind": "configured", "binary": "claude",
               "version_args": ["--version"],
               "invoke_args": ["-p", "--output-format", "json"],
               "write_args": ["--permission-mode", "acceptEdits"],
               "prompt_via": "stdin", "parse": "auto", "model": "auto"},
    "qwen": {"type": "cli", "kind": "configured", "binary": "qwen",
             "version_args": ["--version"], "invoke_args": ["-p"],
             "prompt_via": "stdin", "parse": "auto"},
}


class IO:
    def __init__(self, answers=None, echo=None):
        self.answers = list(answers or [])
        self.lines = []
        self._echo = echo

    def say(self, text):
        self.lines.append(text)
        if self._echo:
            self._echo(text)
        else:
            print(text)

    def ask(self, question, default=None):
        prompt = "%s%s: " % (question,
                             " [%s]" % default if default is not None else "")
        if self.answers:
            answer = str(self.answers.pop(0))
            self.say(prompt + answer)
        else:
            answer = input(prompt)
        answer = answer.strip()
        return answer if answer else (str(default) if default is not None
                                      else "")


def detect_backends(cfg, runner=None, which=None, transport=None):
    """Detect CLIs, Ollama, and configured API backends. Safe: version/auth
    status commands only, no credential files, no paid calls."""
    from providers.cli_codex import CodexCLIBackend
    from providers.cli_configured import ConfiguredCLIBackend
    from providers.local_ollama import detect_ollama

    found = {}
    for name, template in KNOWN_CLIS.items():
        template = dict(template,
                        **((cfg.get("backends") or {}).get(name) or {}))
        cls = CodexCLIBackend if template.get("kind") == "codex" \
            else ConfiguredCLIBackend
        adapter = cls(name, template, runner=runner, which=which)
        info = adapter.detect()
        if info["installed"]:
            info["auth"] = adapter.auth_status()
            info["config"] = template
            found[name] = info
    ollama = detect_ollama(runner=runner, which=which)
    if ollama["installed"]:
        found["ollama"] = dict(ollama, auth="ok")
    apis = {}
    for pname, pcfg in (cfg.get("providers") or {}).items():
        key_env = (pcfg or {}).get("api_key_env")
        if key_env:
            apis[pname] = {"configured": bool(os.environ.get(key_env)),
                           "api_key_env": key_env}
    return found, apis


def run_setup(cfg=None, answers=None, echo=None, runner=None, which=None,
              transport=None, smoke=True, env=None):
    cfg = cfg or config_mod.load_config(env=env)
    io = IO(answers, echo)
    io.say("=== Agentic OS setup ===")
    detected, apis = detect_backends(cfg, runner=runner, which=which)

    io.say("\nDetected backends:")
    for name, info in detected.items():
        io.say("  %-8s version=%s auth=%s%s" % (
            name, info.get("version") or "?", info.get("auth", "?"),
            " models=%s" % ",".join(info.get("models", [])[:5])
            if info.get("models") else ""))
    if not detected:
        io.say("  (no CLI/local backends found)")
    io.say("\nAPI backends (optional — not required for CLI/local mode):")
    for pname, info in apis.items():
        io.say("  %-12s key env %s: %s" % (
            pname, info["api_key_env"],
            "present" if info["configured"] else "not set"))

    machine = {"backends": {}, "routing": {}, "scheduler": {},
               "interaction": {}, "notifications": {}}
    for name, info in detected.items():
        if name == "ollama":
            model = None
            installed = info.get("models") or []
            generation_models = [m for m in installed
                                 if not is_embedding_model(m)]
            if generation_models:
                model = io.ask("Ollama model to use (%s)"
                               % ", ".join(generation_models[:8]),
                               default=generation_models[0])
                # validated against `ollama list`, never a typo or an
                # embedding-only model -- see core.modelres.resolve_model
                if model not in installed:
                    io.say("  warning: %r is not an installed Ollama "
                          "model (installed: %s); using %r instead"
                          % (model, ", ".join(installed) or "none",
                             generation_models[0]))
                    model = generation_models[0]
                elif is_embedding_model(model):
                    io.say("  warning: %r is an embedding-only model and "
                          "cannot serve a generative role; using %r "
                          "instead" % (model, generation_models[0]))
                    model = generation_models[0]
            elif installed:
                io.say("  warning: only embedding models are installed "
                      "(%s); install a generation model with `ollama "
                      "pull <model>` before Ollama can serve a role"
                      % ", ".join(installed))
            machine["backends"]["ollama"] = {
                "type": "local", "model": model,
                "api_key_required": False, "cost_free": True}
        else:
            machine["backends"][name] = info["config"]

    candidates = list(machine["backends"].keys()) + \
        [p for p, i in apis.items() if i["configured"]]
    if not candidates:
        io.say("\nNo usable backend detected. Configure a CLI, Ollama, or an "
               "API key, then re-run setup.")
        return {"ok": False, "reason": "no backends detected",
                "output": io.lines}

    mode = io.ask("\nRouting: simple (one chain for all agents) or per_agent",
                  default="simple")
    primary = io.ask("Primary backend (%s)" % ", ".join(candidates),
                     default=candidates[0])
    fallback_raw = io.ask("Ordered fallbacks, comma-separated (or empty)",
                          default=",".join(c for c in candidates
                                           if c != primary))
    fallbacks = [f.strip() for f in fallback_raw.split(",") if f.strip()]
    machine["routing"] = {"mode": "per_agent" if mode == "per_agent"
                          else "simple",
                          "primary": primary, "fallbacks": fallbacks}
    if mode == "per_agent":
        per = {}
        for role in ("architect", "conductor", "coder", "qa", "security"):
            role_primary = io.ask("  %s primary" % role, default=primary)
            per[role] = {"primary": role_primary, "fallbacks": fallbacks}
        machine["routing"]["per_agent"] = per

    cycle_minutes = io.ask("Cycle target duration minutes", default="20")
    cooling = io.ask("Default cooling period minutes", default="30")
    interaction = io.ask("Interaction mode (cycle_review | milestone_review "
                         "| completion_only)", default="completion_only")
    window = io.ask("Operating hours (e.g. 07:00-22:00, or 'always')",
                    default="always")
    desktop = io.ask("Desktop notifications? (yes/no)", default="yes")

    machine["scheduler"] = {
        "cycle": {"target_duration_minutes": int(float(cycle_minutes)),
                  "maximum_duration_minutes":
                      max(30, int(float(cycle_minutes)) + 10),
                  "maximum_tasks": 1},
        "cooling": {"after_success_minutes": int(float(cooling)),
                    "after_failure_minutes": int(float(cooling))},
    }
    if window != "always" and "-" in window:
        start, stop = window.split("-", 1)
        machine["scheduler"]["operating_window"] = {
            "enabled": True, "start": start.strip(), "stop": stop.strip()}
    machine["interaction"] = {"mode": interaction}
    machine["notifications"] = {"desktop": desktop.lower().startswith("y")}

    # smoke tests -------------------------------------------------------------
    smoke_results = {}
    if smoke:
        io.say("\nRunning safe backend smoke tests...")
        merged = config_mod.deep_merge(cfg, machine)
        for name in machine["backends"]:
            adapter = None
            try:
                adapter = backends_mod.build_backend(merged, name,
                                                     runner=runner,
                                                     which=which,
                                                     transport=transport)
                ok = adapter.smoke_test(str(config_mod.repo_root(cfg)))
            except Exception as exc:
                ok = False
                io.say("  %s: smoke test error: %s" % (name, exc))
            smoke_results[name] = ok
            io.say("  %-8s %s" % (name, "OK" if ok else "FAILED"))
            machine["backends"][name]["smoke_test_passed"] = bool(ok)
            # adapters with structured diagnostics (Codex): surface the
            # reason/exit-code/events on failure and persist for doctor.
            last_smoke = getattr(adapter, "last_smoke", None)
            if last_smoke is not None:
                if not ok:
                    io.say("    reason: %s (exit=%s timeout=%s events=%s)"
                          % (last_smoke.get("reason"),
                             last_smoke.get("exit_code"),
                             last_smoke.get("timed_out"),
                             ",".join(last_smoke.get("event_types") or [])
                             or "-"))
                from . import authx
                authx.record_verification(
                    str(config_mod.AGENTIC_DIR / "memory"), name, ok,
                    last_smoke)

    path = str(config_mod.AGENTIC_DIR / "config.machine.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Machine-local Agentic OS configuration (git-ignored).\n"
                 "# Never store credentials here.\n")
        yaml.safe_dump(machine, fh, sort_keys=False)

    io.say("\n=== Configuration summary ===")
    io.say("primary: %s  fallbacks: %s" % (primary, " -> ".join(fallbacks)
                                           or "(none)"))
    io.say("interaction: %s  cycle: %smin  cooling: %smin"
           % (interaction, cycle_minutes, cooling))
    io.say("written: %s" % path)
    return {"ok": True, "machine": machine, "path": path,
            "smoke": smoke_results, "output": io.lines}
