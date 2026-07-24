"""Project and capability inventory (Phase 1.B).

A versioned, persisted snapshot of "what is actually true about this
repository and this machine right now" -- languages, frameworks, test/
lint/build tooling, Docker/Supabase/migrations, installed CLI backends,
local Ollama models, backend readiness, MCP servers, enabled skills,
writable paths, credential status (never values), OS constraints.

Built once at project start and re-used across cycles; rebuilt only when
`is_stale()` says something that would change it actually changed
(git HEAD moved, a manifest's content hash changed, or the persisted
schema/inventory version is old) -- models are never asked to
rediscover unchanged inventory every cycle.

Every probe here is best-effort and independently wrapped: a failure
probing one subsystem (e.g. MCP gateway unavailable) never blocks the
rest of the inventory from being built."""
import datetime as _dt
import hashlib
import os

from . import gitops, projstate

INVENTORY_VERSION = "1.0"
FILENAME = "inventory.yaml"

# Manifest-shaped files whose CONTENT changing should invalidate the
# inventory (dependencies/tooling may have changed); presence-only
# checks (Dockerfile, supabase/) don't need content hashing.
INVALIDATION_MANIFESTS = (
    "package.json", "pyproject.toml", "requirements.txt", "setup.cfg",
    "Pipfile", "poetry.lock", "go.mod", "Cargo.toml", "Gemfile",
    "composer.json", "pytest.ini", "vitest.config.js", "vitest.config.ts",
    "jest.config.js", "tsconfig.json",
)

LANGUAGE_EXTENSIONS = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".go": "go", ".rs": "rust",
    ".rb": "ruby", ".java": "java", ".cs": "csharp", ".php": "php",
    ".c": "c", ".cpp": "cpp", ".swift": "swift", ".kt": "kotlin",
}


def _path(agentic_dir):
    return os.path.join(projstate.project_dir(agentic_dir), FILENAME)


def load(agentic_dir):
    return projstate.read_yaml(agentic_dir, FILENAME, None)


def save(agentic_dir, inventory):
    projstate.write_yaml(agentic_dir, FILENAME, inventory)
    return inventory


def _current_head(repo_root):
    try:
        return gitops.run_git(["rev-parse", "HEAD"], cwd=repo_root,
                              check=False).strip() or None
    except Exception:   # noqa: BLE001
        return None


def _manifest_fingerprint(repo_root):
    """Cheap content fingerprint of every known manifest that currently
    exists -- order-independent, so adding/removing a manifest also
    changes the fingerprint."""
    parts = []
    for name in sorted(INVALIDATION_MANIFESTS):
        path = os.path.join(repo_root, name)
        if os.path.exists(path):
            try:
                with open(path, "rb") as fh:
                    parts.append(name + ":" + hashlib.sha256(
                        fh.read()).hexdigest()[:16])
            except OSError:
                parts.append(name + ":unreadable")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def is_stale(agentic_dir, repo_root):
    """True when the inventory needs rebuilding: missing, wrong schema
    version, HEAD moved, or a tracked manifest's content changed."""
    existing = load(agentic_dir)
    if not existing or existing.get("inventory_version") != INVENTORY_VERSION:
        return True
    if existing.get("repository_revision") != _current_head(repo_root):
        return True
    if existing.get("manifest_fingerprint") != \
            _manifest_fingerprint(repo_root):
        return True
    return False


# -- individual probes (each independently best-effort) -----------------------

def _probe(fn, default):
    try:
        return fn()
    except Exception as exc:   # noqa: BLE001 -- inventory is best-effort
        return {"error": str(exc)[:200], **({"value": default}
                                            if default is not None else {})}


def _git_state(repo_root):
    branch = gitops.run_git(["rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=repo_root, check=False).strip()
    dirty = bool(gitops.run_git(["status", "--porcelain"], cwd=repo_root,
                                check=False).strip())
    return {"branch": branch or None, "head": _current_head(repo_root),
            "dirty": dirty, "has_commits": gitops.has_commits(repo_root)}


def _languages(repo_root):
    counts = {}
    try:
        tracked = gitops.run_git(["ls-files"], cwd=repo_root,
                                 check=False).splitlines()
    except Exception:   # noqa: BLE001
        tracked = []
    for rel in tracked[:5000]:
        ext = os.path.splitext(rel)[1].lower()
        lang = LANGUAGE_EXTENSIONS.get(ext)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _manifests_and_frameworks(repo_root):
    present = [name for name in INVALIDATION_MANIFESTS
              if os.path.exists(os.path.join(repo_root, name))]
    package_managers = []
    if os.path.exists(os.path.join(repo_root, "package-lock.json")):
        package_managers.append("npm")
    if os.path.exists(os.path.join(repo_root, "yarn.lock")):
        package_managers.append("yarn")
    if os.path.exists(os.path.join(repo_root, "pnpm-lock.yaml")):
        package_managers.append("pnpm")
    if os.path.exists(os.path.join(repo_root, "poetry.lock")):
        package_managers.append("poetry")
    if os.path.exists(os.path.join(repo_root, "requirements.txt")):
        package_managers.append("pip")
    if os.path.exists(os.path.join(repo_root, "go.mod")):
        package_managers.append("go modules")
    if os.path.exists(os.path.join(repo_root, "Cargo.toml")):
        package_managers.append("cargo")
    frameworks = []
    pkg = os.path.join(repo_root, "package.json")
    if os.path.exists(pkg):
        try:
            import json
            with open(pkg, encoding="utf-8") as fh:
                data = json.load(fh)
            deps = dict(data.get("dependencies") or {})
            deps.update(data.get("devDependencies") or {})
            for name in ("react", "vue", "svelte", "next", "express",
                        "vitest", "jest", "mocha"):
                if name in deps:
                    frameworks.append(name)
        except (OSError, ValueError):
            pass
    if os.path.exists(os.path.join(repo_root, "manage.py")):
        frameworks.append("django")
    for req_name in ("requirements.txt", "pyproject.toml"):
        req_path = os.path.join(repo_root, req_name)
        if os.path.exists(req_path):
            try:
                with open(req_path, encoding="utf-8", errors="replace") \
                        as fh:
                    text = fh.read().lower()
                for name in ("fastapi", "flask", "django", "pytest"):
                    if name in text:
                        frameworks.append(name)
            except OSError:
                pass
    return present, sorted(set(package_managers)), sorted(set(frameworks))


def _source_directories(repo_root):
    try:
        entries = os.listdir(repo_root)
    except OSError:
        return []
    skip = {".git", "node_modules", "__pycache__", ".agentic", "dist",
           "build", ".venv", "venv"}
    return sorted(e for e in entries if e not in skip and
                 os.path.isdir(os.path.join(repo_root, e)))


def _test_build_tooling(cfg, repo_root):
    from . import gate
    commands = gate.detect_commands(repo_root)
    by_kind = {}
    for c in commands:
        by_kind.setdefault(c.get("kind", "test_suite"), []).append(
            c["command"])
    return by_kind


def _docker_supabase_migrations(repo_root):
    docker = any(os.path.exists(os.path.join(repo_root, name)) for name in
                ("Dockerfile", "docker-compose.yml", "docker-compose.yaml",
                 "compose.yml", "compose.yaml"))
    supabase = os.path.isdir(os.path.join(repo_root, "supabase"))
    migrations = any(
        os.path.isdir(os.path.join(repo_root, d, "migrations"))
        for d in ("", "supabase", "db") if os.path.isdir(
            os.path.join(repo_root, d)) or d == "")
    return {"docker": docker, "supabase": supabase,
           "migrations_present": migrations}


def _cli_backends(cfg, memory_dir):
    from . import authx
    return authx.read_verification(memory_dir) or {}


def _ollama_models(cfg, memory_dir):
    from . import modelcap
    registry = modelcap.load_registry(memory_dir)
    if registry is None:
        return {"discovered": False, "models": []}
    data = registry.to_dict() if hasattr(registry, "to_dict") else {}
    models = [m for m in (data.get("models") or [])
             if (m.get("backend") or "").lower() == "ollama"]
    return {"discovered": True, "models": models}


def _mcp_servers(cfg, registry_home):
    from . import mcp
    gateway = mcp.MCPGateway(cfg, registry_home)
    return [{k: s.get(k) for k in ("id", "transport", "enabled", "reviewed")}
           for s in gateway.list()]


def _skills(cfg, agentic_dir):
    from . import skillreg
    reg = skillreg.SkillRegistry(cfg, agentic_dir)
    return [{k: s.get(k) for k in ("id", "enabled", "reviewed")}
           for s in reg.list()]


def _writable_paths(repo_root):
    checked = {}
    for rel in (".", "src", "tests"):
        path = os.path.join(repo_root, rel)
        checked[rel] = os.access(path, os.W_OK) if os.path.exists(path) \
            else os.access(repo_root, os.W_OK)
    return checked


def _os_constraints():
    import platform
    return {"os_name": os.name, "platform": platform.system(),
            "case_sensitive_fs": os.name != "nt",
            "path_separator": os.sep}


# -- assembly --------------------------------------------------------------------

def build_inventory(cfg, repo_root, agentic_dir, memory_dir=None,
                    registry_home=None):
    """Build a fresh inventory. Callers should check `is_stale()` first
    and reuse the persisted one otherwise."""
    memory_dir = memory_dir or os.path.join(agentic_dir, "memory")
    manifests, package_managers, frameworks = _probe(
        lambda: _manifests_and_frameworks(repo_root), ([], [], []))
    if isinstance(manifests, dict):   # probe failed
        manifests, package_managers, frameworks = [], [], []
    now = _dt.datetime.now().isoformat(timespec="seconds")
    observed = {
        "git": _probe(lambda: _git_state(repo_root), {}),
        "languages": _probe(lambda: _languages(repo_root), {}),
        "manifests_present": manifests,
        "package_managers": package_managers,
        "frameworks": frameworks,
        "source_directories": _probe(lambda: _source_directories(repo_root),
                                     []),
        "tooling": _probe(lambda: _test_build_tooling(cfg, repo_root), {}),
        "docker_supabase_migrations": _probe(
            lambda: _docker_supabase_migrations(repo_root), {}),
        "cli_backend_verification": _probe(
            lambda: _cli_backends(cfg, memory_dir), {}),
        "ollama_models": _probe(lambda: _ollama_models(cfg, memory_dir),
                                {"discovered": False, "models": []}),
        "mcp_servers": _probe(
            lambda: _mcp_servers(cfg, registry_home) if registry_home
            else [], []),
        "skills": _probe(lambda: _skills(cfg, str(agentic_dir)), []),
        "writable_paths": _probe(lambda: _writable_paths(repo_root), {}),
        "os_constraints": _probe(_os_constraints, {}),
    }
    return {
        "inventory_version": INVENTORY_VERSION,
        "source": "automated_scan",
        "confidence": "observed",
        "observed_at": now,
        "repository_revision": _current_head(repo_root),
        "manifest_fingerprint": _manifest_fingerprint(repo_root),
        "invalidation_triggers": [
            "repository_revision changes (new commit/checkout)",
            "any of %s changes" % ", ".join(INVALIDATION_MANIFESTS),
            "inventory_version bump",
        ],
        "observed": observed,
    }


def ensure_inventory(cfg, repo_root, agentic_dir, memory_dir=None,
                     registry_home=None, force=False):
    """The single entry point callers should use: returns the persisted
    inventory, rebuilding only when stale (or `force`d) -- this is what
    keeps models from re-discovering unchanged repository facts every
    cycle."""
    if not force and not is_stale(agentic_dir, repo_root):
        return load(agentic_dir)
    inventory = build_inventory(cfg, repo_root, agentic_dir,
                                memory_dir=memory_dir,
                                registry_home=registry_home)
    return save(agentic_dir, inventory)
