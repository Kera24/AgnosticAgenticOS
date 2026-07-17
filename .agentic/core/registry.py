"""Central project registry: Agentic OS installed once, managing external
application repositories.

Storage lives in the machine-local runtime home (never inside any project
repository, never in Git):

    %USERPROFILE%\\.agentic-os\\          (override: AGENTIC_HOME)
        registry.json                     the registry itself (schema v1)
        registry.lock                     cross-process write lock
        projects/<id>/                    per-project runtime state
            project/  memory/  runs/  worktrees/  knowledge/  logs/

Rules enforced here:
- ids are stable slugs, unique, assigned once;
- root paths resolve to absolute canonical form (case-insensitive on
  Windows) before any comparison;
- nonexistent roots are rejected unless the user is creating the project;
- when `registry.authorised_roots` is configured, roots outside it are
  rejected; explicit adds are otherwise user authority in themselves;
- the Agentic OS platform repository is refused unless explicitly allowed;
- duplicate canonical roots are rejected;
- no credentials, ever — records hold names/paths/references only;
- writes are atomic (tmp + replace) under a lock file; a corrupt registry
  is preserved (`registry.json.corrupt-<ts>`) and never silently lost;
- removing or archiving a project NEVER touches the application folder.
"""
import datetime as _dt
import json
import os
import re
import time
import uuid

from . import config as config_mod
from . import errors

SCHEMA_VERSION = 1

RECORD_DEFAULTS = {
    "id": None, "name": None, "root_path": None,
    "canonical_root_path": None, "plan_path": "plan.md",
    "git_repository": None, "default_branch": None,
    "agentic_branch": "agentic/project",
    "status": "registered",       # registered|initialised|archived
    "enabled": False, "priority": 50,
    "created_at": None, "updated_at": None, "last_opened_at": None,
    "current_run_id": None, "current_task_id": None,
    "owner_machine": None, "lease": None,
    "backend_profile": None, "context_profile": None,
    "environment_profile": "local",
    "docker_compose_project": None, "supabase_project_ref": None,
    "metadata": {},
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def runtime_home(env=None):
    env = env if env is not None else os.environ
    home = env.get("AGENTIC_HOME")
    if home:
        return os.path.abspath(home)
    return os.path.join(os.path.expanduser("~"), ".agentic-os")


def canonical(path):
    """Absolute, symlink-resolved, case-normalised (Windows) form."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _now():
    return _dt.datetime.now().isoformat(timespec="seconds")


def _slug(name):
    slug = _SLUG_RE.sub("-", (name or "project").lower()).strip("-")
    return slug or "project"


class RegistryError(errors.AgenticError):
    kind = "registry"


class ProjectRegistry:
    def __init__(self, home=None, env=None, clock=None):
        self.home = home or runtime_home(env)
        self.path = os.path.join(self.home, "registry.json")
        self.lock_path = os.path.join(self.home, "registry.lock")
        self.clock = clock or _now

    # -- storage ------------------------------------------------------------
    def _empty(self):
        return {"schema_version": SCHEMA_VERSION, "authorised_roots": [],
                "projects": {}}

    def load(self):
        if not os.path.exists(self.path):
            return self._empty()
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (ValueError, OSError):
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            try:
                os.replace(self.path, self.path + ".corrupt-" + stamp)
            except OSError:
                pass
            return self._empty()
        return self._migrate(data)

    def _migrate(self, data):
        version = int(data.get("schema_version") or 1)
        # future migrations chain here; v1 records just get new defaults
        data["schema_version"] = SCHEMA_VERSION
        data.setdefault("authorised_roots", [])
        data.setdefault("projects", {})
        for record in data["projects"].values():
            for key, default in RECORD_DEFAULTS.items():
                record.setdefault(key, default if not isinstance(
                    default, dict) else dict(default))
        if version > SCHEMA_VERSION:
            raise RegistryError("registry schema %s is newer than this "
                                "Agentic OS (%s)" % (version, SCHEMA_VERSION))
        return data

    def _save(self, data):
        os.makedirs(self.home, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        os.replace(tmp, self.path)

    def _locked(self):
        return _FileLock(self.lock_path)

    # -- queries -------------------------------------------------------------
    def list(self, include_archived=True):
        projects = list(self.load()["projects"].values())
        if not include_archived:
            projects = [p for p in projects if p["status"] != "archived"]
        return sorted(projects, key=lambda p: (-int(p.get("priority") or 0),
                                               p["id"]))

    def get(self, project_id):
        record = self.load()["projects"].get(project_id)
        if record is None:
            raise RegistryError("unknown project %r" % project_id)
        return record

    def find_by_root(self, root):
        want = canonical(root)
        for record in self.load()["projects"].values():
            if record.get("canonical_root_path") == want:
                return record
        return None

    # -- registration ---------------------------------------------------------
    def _check_authorised(self, data, canon, allow_platform):
        platform_root = canonical(str(config_mod.AGENTIC_DIR.parent))
        if canon == platform_root and not allow_platform:
            raise RegistryError(
                "refusing to register the Agentic OS platform repository "
                "itself; pass allow_platform explicitly if intended")
        roots = data.get("authorised_roots") or []
        if roots and not any(
                canon == canonical(r) or
                canon.startswith(canonical(r) + os.sep) for r in roots):
            raise RegistryError(
                "path %s is outside the authorised roots (%s); extend "
                "registry.authorised_roots or register from an authorised "
                "location" % (canon, ", ".join(roots)))

    def add(self, name, root, plan="plan.md", create=False, priority=50,
            allow_platform=False, metadata=None):
        if not name or not str(name).strip():
            raise RegistryError("a project name is required")
        root = os.path.abspath(os.path.expanduser(str(root)))
        if not os.path.isdir(root):
            if not create:
                raise RegistryError(
                    "root %s does not exist (use `project create` to make a "
                    "new project folder)" % root)
            os.makedirs(root, exist_ok=True)
        canon = canonical(root)
        with self._locked():
            data = self.load()
            self._check_authorised(data, canon, allow_platform)
            for record in data["projects"].values():
                if record.get("canonical_root_path") == canon and \
                        record["status"] != "archived":
                    raise RegistryError(
                        "path already registered as project %r"
                        % record["id"])
            base = _slug(name)
            project_id = base
            if project_id in data["projects"]:
                project_id = "%s-%s" % (base, uuid.uuid4().hex[:4])
            now = self.clock()
            record = dict(RECORD_DEFAULTS, metadata=dict(metadata or {}))
            record.update({
                "id": project_id, "name": str(name).strip(),
                "root_path": root, "canonical_root_path": canon,
                "plan_path": plan or "plan.md",
                "git_repository": root if os.path.isdir(
                    os.path.join(root, ".git")) else None,
                "docker_compose_project": "agentic-" + project_id,
                "owner_machine": os.environ.get("COMPUTERNAME")
                or os.environ.get("HOSTNAME"),
                "created_at": now, "updated_at": now,
            })
            data["projects"][project_id] = record
            self._save(data)
        self.ensure_runtime_dirs(project_id)
        return record

    def update(self, project_id, **fields):
        forbidden = {"id", "created_at", "canonical_root_path"} & set(fields)
        if forbidden:
            raise RegistryError("fields %s are immutable" % sorted(forbidden))
        with self._locked():
            data = self.load()
            record = data["projects"].get(project_id)
            if record is None:
                raise RegistryError("unknown project %r" % project_id)
            unknown = set(fields) - set(RECORD_DEFAULTS)
            if unknown:
                raise RegistryError("unknown fields %s" % sorted(unknown))
            record.update(fields)
            record["updated_at"] = self.clock()
            self._save(data)
            return record

    def relink(self, project_id, new_root, allow_platform=False):
        """Point an existing project at a moved folder. The id, history and
        runtime state are preserved."""
        new_root = os.path.abspath(os.path.expanduser(str(new_root)))
        if not os.path.isdir(new_root):
            raise RegistryError("new root %s does not exist" % new_root)
        canon = canonical(new_root)
        with self._locked():
            data = self.load()
            record = data["projects"].get(project_id)
            if record is None:
                raise RegistryError("unknown project %r" % project_id)
            self._check_authorised(data, canon, allow_platform)
            for other_id, other in data["projects"].items():
                if other_id != project_id and \
                        other.get("canonical_root_path") == canon and \
                        other["status"] != "archived":
                    raise RegistryError("path already registered as %r"
                                        % other_id)
            record["root_path"] = new_root
            record["canonical_root_path"] = canon
            record["git_repository"] = new_root if os.path.isdir(
                os.path.join(new_root, ".git")) else None
            record["updated_at"] = self.clock()
            self._save(data)
            return record

    def archive(self, project_id):
        """Stop managing the project; application files stay untouched and
        the record stays for history."""
        return self.update(project_id, status="archived", enabled=False)

    def remove(self, project_id):
        """Delete the registry record (and NOTHING else — the application
        folder and even the runtime state are left on disk)."""
        with self._locked():
            data = self.load()
            if project_id not in data["projects"]:
                raise RegistryError("unknown project %r" % project_id)
            del data["projects"][project_id]
            self._save(data)
        return {"removed": project_id,
                "note": "application folder and runtime state were NOT "
                        "deleted"}

    def authorise_root(self, root):
        with self._locked():
            data = self.load()
            root = os.path.abspath(os.path.expanduser(str(root)))
            if root not in data["authorised_roots"]:
                data["authorised_roots"].append(root)
                self._save(data)
            return data["authorised_roots"]

    # -- runtime paths ---------------------------------------------------------
    def project_runtime_dir(self, project_id):
        return os.path.join(self.home, "projects", project_id)

    def ensure_runtime_dirs(self, project_id):
        base = self.project_runtime_dir(project_id)
        for sub in ("project", "memory", "runs", "worktrees", "knowledge",
                    "logs", "queue"):
            os.makedirs(os.path.join(base, sub), exist_ok=True)
        return base

    def project_cfg(self, base_cfg, record_or_id):
        """Overlay a base configuration for one registered project. The
        existing engine then runs unchanged against redirected paths."""
        import copy
        record = record_or_id if isinstance(record_or_id, dict) \
            else self.get(record_or_id)
        cfg = copy.deepcopy(base_cfg)
        cfg.setdefault("project", {})
        cfg["project"]["name"] = record["id"]
        cfg["project"]["repository_root"] = record["root_path"]
        cfg["runtime"] = {
            "project_id": record["id"],
            "project_dir": self.project_runtime_dir(record["id"]),
            "home": self.home,
        }
        profile = record.get("backend_profile")
        if isinstance(profile, dict):
            from .config import deep_merge
            cfg = deep_merge(cfg, profile)
        return cfg


class _FileLock:
    """Small cross-process lock (O_CREAT|O_EXCL with staleness)."""

    def __init__(self, path, timeout=10.0, stale_seconds=60):
        self.path = path
        self.timeout = timeout
        self.stale_seconds = stale_seconds
        self.fd = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.time() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path,
                                  os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.path)
                    if age > self.stale_seconds:
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise RegistryError("registry lock held too long: %s"
                                        % self.path)
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            os.remove(self.path)
        except OSError:
            pass
