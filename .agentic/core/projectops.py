"""Operations on registered projects: initialisation, doctor, lifecycle.

Everything here is deterministic — `project init` prepares Git, runtime
state, the code index, the memory namespace and the knowledge vault
WITHOUT any model call. The architect runs later via `project start` /
the scheduler, through the existing engine.
"""
import os

from . import config as config_mod
from . import gitops, projstate
from .registry import ProjectRegistry, RegistryError

PLAN_CANDIDATES = ("plan.md", "PLAN.md", "Plan.md", "docs/plan.md",
                   "plan.txt")


def resolve_project(registry, ident=None, cwd=None):
    """Resolve a project by id, or (convenience only) by the current
    directory if it is already registered."""
    if ident:
        return registry.get(ident)
    probe = cwd or os.getcwd()
    record = registry.find_by_root(probe)
    if record is None:
        raise RegistryError(
            "no project id given and the current directory is not a "
            "registered project (use `project list`)")
    return record


def project_cfg_for(cfg, registry, record):
    """Config overlay for a record; legacy-adopted projects keep their
    state where it already lives (inside the platform .agentic dir)."""
    overlaid = registry.project_cfg(cfg, record)
    if (record.get("metadata") or {}).get("legacy"):
        overlaid["runtime"]["project_dir"] = str(config_mod.AGENTIC_DIR)
    return overlaid


def find_plan(record):
    root = record["root_path"]
    configured = os.path.join(root, record.get("plan_path") or "plan.md")
    if os.path.isfile(configured):
        return configured
    for candidate in PLAN_CANDIDATES:
        path = os.path.join(root, candidate)
        if os.path.isfile(path):
            return path
    return None


def detect_integrations(root):
    """Docker/Supabase presence detection (expanded by their adapters)."""
    exists = lambda *p: os.path.exists(os.path.join(root, *p))  # noqa: E731
    supabase_ref = None
    ref_path = os.path.join(root, "supabase", ".temp", "project-ref")
    if os.path.isfile(ref_path):
        try:
            with open(ref_path, encoding="utf-8") as fh:
                supabase_ref = fh.read().strip()[:64] or None
        except OSError:
            pass
    return {
        "docker": exists("docker-compose.yml") or exists("compose.yaml")
        or exists("compose.yml") or exists("Dockerfile"),
        "supabase": exists("supabase", "config.toml"),
        "supabase_migrations": exists("supabase", "migrations"),
        "supabase_project_ref": supabase_ref,
    }


def project_init(cfg, registry, project_id, log=None):
    """Idempotent, model-free initialisation. Returns a step report."""
    log = log or (lambda e: None)
    record = registry.get(project_id)
    root = record["root_path"]
    steps, warnings = {}, []

    # 1–2. git + ownership/permissions
    if not os.path.isdir(root):
        raise RegistryError("project root %s no longer exists (relink?)"
                            % root)
    if not os.access(root, os.W_OK):
        raise RegistryError("project root %s is not writable" % root)
    if not gitops.is_repo(root):
        gitops.run_git(["init", "-b", "main"], cwd=root)
        steps["git_init"] = True
    else:
        steps["git_init"] = False
    toplevel = gitops.run_git(["rev-parse", "--show-toplevel"], cwd=root,
                              check=False).strip()
    from .registry import canonical
    if toplevel and canonical(toplevel) != canonical(root):
        raise RegistryError(
            "root %s is inside another git repository (%s); register the "
            "repository root instead" % (root, toplevel))
    if not gitops.has_commits(root):
        result = gitops.run_git(
            ["-c", "user.name=Agentic OS", "-c",
             "user.email=agentic-os@localhost", "commit", "--allow-empty",
             "-m", "agentic: initial commit"], cwd=root, check=False)
        steps["initial_commit"] = gitops.has_commits(root)
        if not steps["initial_commit"]:
            warnings.append("could not create an initial commit: %s"
                            % result.strip()[:200])
    else:
        steps["initial_commit"] = False

    default_branch = gitops.run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"], cwd=root,
        check=False).strip() or None

    # 4. plan
    plan = find_plan(record)
    steps["plan"] = plan
    if plan is None:
        warnings.append("no plan found (looked for %s under %s); add one "
                        "before `project start`"
                        % (record.get("plan_path"), root))
    elif os.path.relpath(plan, root) != record.get("plan_path"):
        registry.update(project_id,
                        plan_path=os.path.relpath(plan, root)
                        .replace("\\", "/"))

    # 5. runtime state dirs
    runtime_dir = registry.ensure_runtime_dirs(project_id)
    steps["runtime_dir"] = runtime_dir

    proj_cfg = project_cfg_for(cfg, registry, registry.get(project_id))
    memory_dir = os.path.join(runtime_dir, "memory")

    # 6. code index (best-effort)
    try:
        from .codeintel import get_adapter
        adapter = get_adapter(proj_cfg, root, memory_dir)
        steps["code_index"] = adapter.index_full()
    except Exception as exc:   # noqa: BLE001
        steps["code_index"] = {"ok": False, "detail": str(exc)[:200]}

    # 7. memory namespace
    try:
        from .memsvc import get_memory
        steps["memory"] = get_memory(proj_cfg, memory_dir).status()
    except Exception as exc:   # noqa: BLE001
        steps["memory"] = {"error": str(exc)[:200]}

    # 8. knowledge vault
    try:
        from .knowledge import update_knowledge
        results = update_knowledge(proj_cfg, runtime_dir, log) or {}
        steps["knowledge"] = {"documents": len(results)}
    except Exception as exc:   # noqa: BLE001
        steps["knowledge"] = {"error": str(exc)[:200]}

    # 9. docker / supabase detection
    integrations = detect_integrations(root)
    steps["integrations"] = integrations
    registry.update(project_id,
                    git_repository=root,
                    default_branch=default_branch,
                    supabase_project_ref=integrations.get(
                        "supabase_project_ref"),
                    status="initialised")

    # 10. project doctor
    doctor = project_doctor(cfg, registry, project_id)
    steps["doctor"] = doctor
    log({"event": "project_initialised", "project": project_id,
         "warnings": warnings})
    return {"project": project_id, "root": root, "steps": steps,
            "warnings": warnings, "ok": not doctor["errors"]}


def project_doctor(cfg, registry, project_id):
    """Per-project readiness checks (no model calls, no network)."""
    record = registry.get(project_id)
    root = record["root_path"]
    checks, errors_, warnings = [], [], []

    def check(ok, message, warn_only=False):
        level = "ok" if ok else ("warn" if warn_only else "error")
        checks.append((level, message))
        if level == "error":
            errors_.append(message)
        elif level == "warn":
            warnings.append(message)

    check(os.path.isdir(root), "root exists: %s" % root)
    if os.path.isdir(root):
        check(os.access(root, os.W_OK), "root writable")
        check(gitops.is_repo(root), "git repository present")
        if gitops.is_repo(root):
            check(gitops.has_commits(root),
                  "repository has commits (worktrees possible)")
            dirty = gitops.run_git(["status", "--porcelain"], cwd=root,
                                   check=False).strip()
            check(not dirty, "working tree clean", warn_only=True)
    plan = find_plan(record)
    check(plan is not None, "plan file present (%s)"
          % (plan or record.get("plan_path")), warn_only=True)
    runtime_dir = registry.project_runtime_dir(project_id)
    check(os.path.isdir(runtime_dir), "runtime state dir: %s" % runtime_dir)
    proj_cfg = project_cfg_for(cfg, registry, record)
    state_dir = (proj_cfg.get("runtime") or {}).get("project_dir")
    started = projstate.exists(state_dir) if state_dir else False
    checks.append(("ok" if started else "warn",
                   "orchestration state %s"
                   % ("present (architected)" if started
                      else "not started yet (run `project start`)")))
    integrations = detect_integrations(root) if os.path.isdir(root) else {}
    checks.append(("ok", "docker: %s · supabase: %s"
                   % ("detected" if integrations.get("docker") else "none",
                      "detected" if integrations.get("supabase")
                      else "none")))
    return {"project": project_id, "checks": checks, "errors": errors_,
            "warnings": warnings, "ok": not errors_}


def project_status(cfg, registry, project_id):
    from .scheduler import Scheduler
    record = registry.get(project_id)
    proj_cfg = project_cfg_for(cfg, registry, record)
    runtime_dir = (proj_cfg.get("runtime") or {}).get("project_dir")
    memory_dir = os.path.join(runtime_dir, "memory")
    scheduler = Scheduler(proj_cfg, memory_dir)
    progress = projstate.read_yaml(runtime_dir, "progress.yaml", {}) or {}
    return {"record": record, "scheduler": scheduler.state,
            "progress": progress,
            "blockers": projstate.open_blockers(runtime_dir)
            if projstate.exists(runtime_dir) else [],
            "plan": find_plan(record),
            "worktree": os.path.join(runtime_dir, "worktrees", "project")}


def adopt_legacy(cfg, registry, name="platform-legacy"):
    """Register the platform repository's implicit single project so its
    existing in-place state keeps working under an id."""
    root = str(config_mod.repo_root(cfg))
    existing = registry.find_by_root(root)
    if existing:
        return existing
    record = registry.add(name, root, allow_platform=True,
                          metadata={"legacy": True})
    if projstate.exists(str(config_mod.AGENTIC_DIR)):
        registry.update(record["id"], status="initialised")
        record = registry.get(record["id"])
    return record
