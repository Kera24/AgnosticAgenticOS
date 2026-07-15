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
        add("warn", "pricing table is empty; with unknown_price_policy=block "
            "only cost_free providers can run")
    return _finish(checks)


def _finish(checks):
    ok = not any(level == "error" for level, _ in checks)
    return ok, checks


def render(checks):
    icon = {"ok": "[ok]  ", "warn": "[warn]", "error": "[FAIL]"}
    return "\n".join(icon[level] + " " + msg for level, msg in checks)
