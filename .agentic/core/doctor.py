"""Preflight validation: dependencies, configuration, providers, repository
state, and required environment variables. Reports presence of secrets, never
values."""
import os
import shutil
import sys

from . import gate, gitops
from .config import AGENTIC_DIR, load_config, repo_root
from .schema import load_schema

REQUIRED_SCHEMAS = ["triage.schema.json", "work-order.schema.json",
                    "verification.schema.json", "worker.schema.json"]
REQUIRED_PROMPTS = ["shared-autonomy.md", "shared-scope.md", "triage.md",
                    "conductor.md", "implement.md", "verify.md"]
CORE_ROLES = ["triage", "conductor", "worker", "verifier"]


def run_doctor(cfg=None, env=None):
    env = env if env is not None else os.environ
    checks = []  # (level: ok|warn|error, message)
    add = lambda level, msg: checks.append((level, msg))

    add("ok" if sys.version_info >= (3, 8) else "error",
        "python %d.%d" % sys.version_info[:2])
    add("ok" if shutil.which("git") else "error", "git executable")
    try:
        import yaml  # noqa: F401
        add("ok", "pyyaml importable")
    except ImportError:
        add("error", "pyyaml missing (pip install pyyaml)")

    try:
        cfg = cfg or load_config(env=env)
        add("ok", "config.yaml parses")
    except Exception as exc:
        add("error", "config.yaml failed to load: %s" % exc)
        return _finish(checks)

    roles = cfg.get("roles", {}) or {}
    providers = cfg.get("providers", {}) or {}
    for role in CORE_ROLES:
        if role not in roles:
            add("error", "role %r missing from config" % role)
    for name, rcfg in roles.items():
        pname = (rcfg or {}).get("provider")
        if pname not in providers:
            add("error", "role %s references unknown provider %r" % (name, pname))
            continue
        fb = rcfg.get("fallback_role")
        if fb and fb not in roles:
            add("error", "role %s references unknown fallback_role %r" % (name, fb))
        model = str(rcfg.get("model", ""))
        if not model:
            add("error", "role %s has no model" % name)
        elif model.startswith(("example", "configurable")):
            add("warn", "role %s still uses placeholder model %r" % (name, model))

    needed_env = set()
    for name, rcfg in roles.items():
        pcfg = providers.get((rcfg or {}).get("provider")) or {}
        if pcfg.get("api_key_required", True) and pcfg.get("api_key_env"):
            needed_env.add(pcfg["api_key_env"])
        if pcfg.get("base_url_env"):
            needed_env.add(pcfg["base_url_env"])
    for var in sorted(needed_env):
        add("ok" if env.get(var) else "warn",
            "env %s %s" % (var, "present" if env.get(var) else "NOT SET"))

    for fname in REQUIRED_SCHEMAS:
        path = AGENTIC_DIR / "schemas" / fname
        try:
            load_schema(str(path))
            add("ok", "schema %s" % fname)
        except Exception as exc:
            add("error", "schema %s unreadable: %s" % (fname, exc))
    for fname in REQUIRED_PROMPTS:
        if (AGENTIC_DIR / "prompts" / fname).exists():
            add("ok", "prompt %s" % fname)
        else:
            add("error", "prompt %s missing" % fname)

    root = repo_root(cfg)
    if gitops.is_repo(str(root)):
        add("ok", "git repository at %s" % root)
        if gitops.has_commits(str(root)):
            add("ok", "repository has commits (worktrees possible)")
        else:
            add("warn", "repository has no commits yet; make an initial commit "
                "before agent-tick (worktrees need HEAD)")
    else:
        add("error", "%s is not a git repository" % root)

    commands, auto = gate.resolve_commands(cfg, str(root))
    if commands:
        add("ok", "deterministic checks (%s): %s"
            % ("auto-detected" if auto else "configured",
               ", ".join(c["name"] for c in commands)))
    else:
        add("warn", "no deterministic checks detected; configure "
            "verification.commands")
    if gate.load_baseline(str(AGENTIC_DIR)) is None:
        add("warn", "no check baseline recorded yet (recorded on first tick)")

    policy = str(cfg.get("budget", {}).get("unknown_price_policy", "block"))
    if policy not in ("block", "warn", "allow"):
        add("error", "budget.unknown_price_policy must be block|warn|allow")
    if not cfg.get("pricing"):
        add("warn", "pricing table is empty; API backends with "
            "unknown_price_policy=block cannot run (CLI/local backends are "
            "unaffected)")

    _backend_checks(cfg, add, env)
    return _finish(checks)


def _backend_checks(cfg, add, env):
    """CLI/local/API backend, routing, scheduler, project, breaker and
    capacity reporting. Auth status via safe CLI status commands only."""
    from . import projstate
    from .breaker import BreakerBoard
    from .scheduler import Scheduler
    from .setupwiz import detect_backends

    try:
        detected, apis = detect_backends(cfg)
    except Exception as exc:
        add("warn", "backend detection failed: %s" % exc)
        detected, apis = {}, {}
    for name, info in detected.items():
        auth = info.get("auth", "?")
        level = "ok" if auth == "ok" else "warn"
        add(level, "backend %s: installed (version %s), auth %s%s" % (
            name, info.get("version") or "?", auth,
            ", models: %s" % ", ".join(info.get("models", [])[:5])
            if info.get("models") else ""))
    if not detected:
        add("warn", "no CLI/local backends detected (API-only mode)")
    for pname, info in apis.items():
        add("ok" if info["configured"] else "warn",
            "api backend %s: key %s %s" % (
                pname, info["api_key_env"],
                "present" if info["configured"] else "NOT SET"))

    routing = cfg.get("routing") or {}
    if routing.get("primary"):
        add("ok", "routing: primary=%s fallbacks=%s (mode=%s)" % (
            routing["primary"], "->".join(routing.get("fallbacks") or []),
            routing.get("mode", "simple")))
    else:
        add("warn", "routing.primary not configured -- run "
            "`python .agentic/run setup`")

    memory = str(AGENTIC_DIR / "memory")
    board = BreakerBoard(memory)
    for backend, entry in board.data.items():
        state = entry.get("state", "?")
        add("ok" if state in ("available", "degraded") else "warn",
            "breaker %s: %s%s" % (backend, state,
                                  " until %s" % entry["unavailable_until"]
                                  if entry.get("unavailable_until") else ""))
    scheduler = Scheduler(cfg, memory)
    add("ok", "scheduler: state=%s next_run_at=%s project=%s" % (
        scheduler.state.get("state"), scheduler.state.get("next_run_at"),
        scheduler.state.get("project_status")))
    if projstate.exists(AGENTIC_DIR):
        progress = projstate.read_yaml(AGENTIC_DIR, "progress.yaml", {}) or {}
        add("ok", "project: %s tasks, status %s" % (
            progress.get("tasks_total", "?"),
            progress.get("tasks_by_status", {})))
    else:
        add("ok", "project: none started (use project-start <plan.md>)")

    from .capacity import CapacityLedger
    ledger = CapacityLedger(cfg, memory)
    has_history = bool(ledger.recent_cycles(limit=1))
    add("ok", "capacity confidence: %s (estimates are local approximations, "
        "never provider-reported quota)"
        % ("estimated (history available)" if has_history else "unknown"))

    usable = bool(detected) or any(i["configured"] for i in apis.values())
    if usable and routing.get("primary"):
        add("ok", "autonomous operation: READY (non-interactive, "
            "workspace-scoped)")
    else:
        add("warn", "autonomous operation: NOT READY -- warnings above must "
            "be resolved first (warnings are not readiness)")


def _finish(checks):
    ok = not any(level == "error" for level, _ in checks)
    return ok, checks


def render(checks):
    icon = {"ok": "[ok]  ", "warn": "[warn]", "error": "[FAIL]"}
    return "\n".join(icon[level] + " " + msg for level, msg in checks)
