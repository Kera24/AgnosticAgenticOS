"""Preflight validation: dependencies, configuration, providers, repository
state, and required environment variables. Reports presence of secrets, never
values."""
import os
import shutil
import sys

from . import gate, gitops
from .config import AGENTIC_DIR, load_config, repo_root
from .modelres import is_placeholder_model
from .schema import load_schema

REQUIRED_SCHEMAS = ["triage.schema.json", "work-order.schema.json",
                    "verification.schema.json", "worker.schema.json"]
REQUIRED_PROMPTS = ["shared-autonomy.md", "shared-scope.md", "triage.md",
                    "conductor.md", "implement.md", "verify.md"]
CORE_ROLES = ["triage", "conductor", "worker", "verifier"]

# Roles the PROJECT engine (project_start/project_run, i.e. `project start`)
# actually calls -- see core/project.py. This is a distinct role set from
# CORE_ROLES above (the legacy repository-maintenance `tick` roles); a
# registered project never touches cfg["roles"] at all.
PROJECT_ROLES = ["architect", "conductor", "coder", "qa", "security"]


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
    migration = cfg.get("_migration") or {}
    if migration.get("sections_filled"):
        add("ok", "config version %s (defaults applied for: %s)"
            % (migration.get("to_version"),
               ", ".join(migration["sections_filled"])))
    else:
        add("ok", "config version %s" % migration.get("to_version", "?"))

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
        model = rcfg.get("model")
        if is_placeholder_model(model):
            add("warn" if model else "error",
                "role %s still uses placeholder model %r" % (name, model)
                if model else "role %s has no model" % name)

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

    try:
        from .codeintel import ci_config, get_adapter
        cicfg = ci_config(cfg)
        adapter = get_adapter(cfg, str(root),
                              str(AGENTIC_DIR / "memory"))
        health = adapter.health_check()
        detail = "code intelligence: configured=%s active=%s" \
            % (cicfg["provider"], adapter.provider_name)
        reason = getattr(adapter, "fallback_reason", None)
        if reason:
            detail += " (%s)" % reason
        add("ok" if health.get("ok") else "warn", detail)
    except Exception as exc:
        add("warn", "code intelligence unavailable: %s" % exc)

    try:
        from .capability import load_taxonomy
        taxonomy = load_taxonomy(agentic_dir=AGENTIC_DIR)
        violations = taxonomy.validate()
        if violations:
            add("error", "capability taxonomy: %d capabilities, %d "
                "categories, %d validation violation(s) -- first: %s"
                % (len(taxonomy.capabilities), len(taxonomy.categories),
                   len(violations), violations[0]))
        else:
            add("ok", "capability taxonomy: version %s, %d capabilities, "
                "%d categories, valid"
                % (taxonomy.taxonomy_version, len(taxonomy.capabilities),
                   len(taxonomy.categories)))
    except Exception as exc:
        # missing (not merely invalid) taxonomy data is a platform-rollout
        # state, not a broken platform -- see "doctor readiness tiers"
        add("warn", "capability taxonomy unavailable: %s" % exc)

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
    memory = str(AGENTIC_DIR / "memory")
    try:
        from .authx import backend_auth_report
        auth_reports = backend_auth_report(cfg, memory, env=env)
    except Exception as exc:   # noqa: BLE001
        auth_reports = {}
        add("warn", "auth detection failed: %s" % exc)
    for name, info in detected.items():
        report = auth_reports.get(name) or {}
        state = report.get("state") or info.get("auth", "?")
        smoke = report.get("smoke_test")
        ready = report.get("autonomous_ready")
        smoke_passed = (smoke or {}).get("ok")
        level = "ok" if state in ("authenticated", "local_ok") else "warn"
        if smoke is not None and not smoke_passed:
            level = "warn"   # a recorded smoke FAILURE always surfaces
        line = ("backend %s: installed (version %s) · auth %s%s · smoke %s"
                " · autonomous %s%s"
                % (name, info.get("version") or "?", state,
                   " (%s)" % report["method"] if report.get("method")
                   else "",
                   "pass" if smoke_passed
                   else ("fail" if smoke else "not recorded"),
                   "READY" if ready else "not ready",
                   ", models: %s" % ", ".join(info.get("models", [])[:5])
                   if info.get("models") else ""))
        add(level, line)
        smoke_detail = (smoke or {}).get("detail")
        if isinstance(smoke_detail, dict):
            add(level, "backend %s: smoke exit=%s timeout=%s events=%s"
                " reason=%s" % (
                    name, smoke_detail.get("exit_code"),
                    smoke_detail.get("timed_out"),
                    ",".join(smoke_detail.get("event_types") or []) or "-",
                    smoke_detail.get("reason", "?")))
        if report.get("credential_conflict"):
            add("warn", "backend %s: %s" % (name,
                                            report["credential_conflict"]))
        if report.get("instructions") and level == "warn":
            add("warn", "backend %s: %s" % (name, report["instructions"]))
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

    from .capacity import CapacityLedger
    board = BreakerBoard(memory)
    ledger = CapacityLedger(cfg, memory)
    project_ready = _project_model_resolution(cfg, add, board, ledger,
                                              detected, memory)

    try:
        from .modelcap import discover_registry, save_registry
        model_registry = discover_registry(
            cfg, memory_dir=memory, detected=detected, apis=apis,
            auth_reports=auth_reports)
        save_registry(memory, model_registry)
        by_class = {}
        for r in model_registry.records:
            if r["available"]:
                by_class.setdefault(r["reasoning_class"], []).append(
                    "%s/%s" % (r["backend"], r["model_id"]))
        summary = ", ".join("%s: %s" % (cls, ", ".join(models[:3]))
                            for cls, models in sorted(by_class.items()))
        add("ok" if by_class else "warn",
            "model capability registry: %s"
            % (summary or "no available models discovered"))
    except Exception as exc:   # noqa: BLE001
        add("warn", "model capability registry unavailable: %s" % exc)
        model_registry = None

    if model_registry is not None:
        try:
            from .capacity import CapacityLedger
            from .hierarchy import frontier_capacity_status
            status, detail = frontier_capacity_status(
                cfg, CapacityLedger(cfg, memory), model_registry)
            add("ok" if status == "ok" else "warn",
                "frontier capacity: %s (reserve=%s%% worker_share=%.0f%% "
                "over %s calls)" % (status, detail["reserve_percent"],
                                    detail["worker_share"] * 100,
                                    detail["total"]))
        except Exception as exc:   # noqa: BLE001
            add("warn", "frontier capacity status unavailable: %s" % exc)

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

    has_history = bool(ledger.recent_cycles(limit=1))
    add("ok", "capacity confidence: %s (estimates are local approximations, "
        "never provider-reported quota)"
        % ("estimated (history available)" if has_history else "unknown"))

    usable = bool(detected) or any(i["configured"] for i in apis.values())
    if usable and routing.get("primary") and project_ready:
        add("ok", "autonomous operation: READY (non-interactive, "
            "workspace-scoped)")
    else:
        add("warn", "autonomous operation: NOT READY -- warnings above must "
            "be resolved first (warnings are not readiness)")


def _project_model_resolution(cfg, add, board, ledger, detected, memory):
    """Resolve, for every role the PROJECT engine actually calls, the exact
    same backend chain + model that `project start`/`project-run` will use
    (core.backends.routing_chain + core.modelres.resolve_model) -- so
    doctor can never again report READY while the first real call fails
    with model_unavailable. Returns True iff every role's primary backend
    resolved to a valid model."""
    from . import backends as backends_mod
    from .modelres import resolve_model

    routing = cfg.get("routing") or {}
    if not routing.get("primary") and routing.get("mode") != "capability" \
            and not routing.get("per_agent"):
        return False   # nothing configured yet; the routing warning above covers it

    roles_cfg = cfg.get("roles") or {}
    backends_cfg = cfg.get("backends") or {}
    all_valid = True
    seen_fallbacks = set()
    for role in PROJECT_ROLES:
        try:
            chain = backends_mod.routing_chain(cfg, role, memory_dir=memory,
                                               board=board, ledger=ledger)
        except Exception as exc:   # noqa: BLE001
            add("warn", "role %s: no usable backend chain (%s)"
                % (role, str(exc)[:150]))
            all_valid = False
            continue
        if not chain:
            add("warn", "role %s: routing produced no candidates" % role)
            all_valid = False
            continue
        primary = chain[0]
        bcfg = backends_cfg.get(primary) or {}
        resolution = resolve_model(
            role, bcfg.get("type", "api"), primary,
            role_model=(roles_cfg.get(role) or {}).get("model"),
            backend_model=bcfg.get("model"),
            detected_models=(detected.get(primary) or {}).get("models"),
            backend_kind=bcfg.get("kind"))
        level = "ok" if resolution["valid"] else "warn"
        if not resolution["valid"]:
            all_valid = False
        add(level, "role %s -> backend %s -> model %s [source=%s] (%s)"
            % (role, primary, resolution["resolved_model"],
               resolution["model_source"], resolution["explanation"]))
        for fb in chain[1:]:
            if fb in seen_fallbacks:
                continue
            seen_fallbacks.add(fb)
            fbcfg = backends_cfg.get(fb) or {}
            fb_resolution = resolve_model(
                role, fbcfg.get("type", "api"), fb,
                role_model=(roles_cfg.get(role) or {}).get("model"),
                backend_model=fbcfg.get("model"),
                detected_models=(detected.get(fb) or {}).get("models"),
                backend_kind=fbcfg.get("kind"))
            add("ok" if fb_resolution["valid"] else "warn",
                "fallback %s -> model %s (%s)"
                % (fb, fb_resolution["resolved_model"],
                   fb_resolution["explanation"]))
    return all_valid


def _finish(checks):
    ok = not any(level == "error" for level, _ in checks)
    return ok, checks


def render(checks):
    icon = {"ok": "[ok]  ", "warn": "[warn]", "error": "[FAIL]"}
    return "\n".join(icon[level] + " " + msg for level, msg in checks)
