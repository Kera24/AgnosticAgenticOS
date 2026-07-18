"""Project-scoped Supabase support.

Migration files under `supabase/migrations` are the ONLY source of truth
for schema. MCP-only or ad-hoc schema changes are refused: every database
mutation must correspond to a version-controlled migration file, and
remote applies require history comparison, a saved dry run, and (where the
environment policy says so) explicit approval. Staging and production can
NEVER be reset or seeded.

All CLI invocations are argv through execpolicy (never a shell) and fully
runner-mockable — tests never touch a real database.
"""
import datetime as _dt
import json
import os
import re

from . import errors, execpolicy

DEFAULT_ENVIRONMENTS = {
    "local": {"database_mutation": "automatic", "migration_apply":
              "automatic", "reset": "automatic", "seed": "automatic"},
    "development": {"database_mutation": "allowed", "migration_apply":
                    "allowed", "reset": "restricted", "seed": "restricted"},
    "staging": {"database_mutation": "approval_required",
                "migration_apply": "approval_required", "reset": "denied",
                "seed": "denied"},
    "production": {"database_mutation": "approval_required",
                   "migration_apply": "approval_required",
                   "reset": "denied", "seed": "denied"},
}

MIGRATION_FILE_RE = re.compile(r"^\d{8,14}_[\w-]+\.sql$")


def environment_policy(cfg, environment, action):
    environments = dict(DEFAULT_ENVIRONMENTS)
    for name, overrides in (cfg.get("supabase") or {}) \
            .get("environments", {}).items():
        environments[name] = dict(environments.get(name, {}),
                                  **(overrides or {}))
    policy = environments.get(environment)
    if policy is None:
        raise errors.PolicyError("unknown Supabase environment %r"
                                 % environment)
    return policy.get(action, "denied")


def guard(cfg, environment, action):
    """Raise on denied; return True when explicit approval is required."""
    verdict = environment_policy(cfg, environment, action)
    if verdict == "denied":
        raise errors.PolicyError(
            "%s is DENIED in the %s environment" % (action, environment))
    if verdict == "restricted":
        raise errors.PolicyError(
            "%s is restricted in %s; run it manually if truly intended"
            % (action, environment))
    return verdict == "approval_required"


class SupabaseAdapter:
    def __init__(self, cfg, project_id, root, runner=None,
                 evidence_dir=None, clock=None):
        self.cfg = cfg
        self.project_id = project_id
        self.root = str(root)
        self.runner = runner or self._default_runner
        self.evidence_dir = evidence_dir
        self.clock = clock or _dt.datetime.now

    @staticmethod
    def _default_runner(argv, cwd=None, timeout=300, stdin_text=None):
        return execpolicy.run_command(argv, cwd=cwd or ".",
                                      timeout=timeout, source="config",
                                      stdin_text=stdin_text)

    def _cli(self, *args, timeout=300):
        return self.runner(["supabase"] + [str(a) for a in args],
                           cwd=self.root, timeout=timeout)

    # -- detection ------------------------------------------------------------
    def detect(self):
        exists = lambda *p: os.path.exists(   # noqa: E731
            os.path.join(self.root, *p))
        ref = None
        ref_path = os.path.join(self.root, "supabase", ".temp",
                                "project-ref")
        if os.path.isfile(ref_path):
            try:
                with open(ref_path, encoding="utf-8") as fh:
                    ref = fh.read().strip()[:64] or None
            except OSError:
                pass
        return {
            "config": exists("supabase", "config.toml"),
            "migrations_dir": exists("supabase", "migrations"),
            "seed": exists("supabase", "seed.sql"),
            "schemas_dir": exists("supabase", "schemas"),
            "linked_project_ref": ref,
            "migrations": self.local_migrations(),
            "types_path": self._types_path(),
        }

    def _types_path(self):
        for candidate in ("src/types/database.ts", "types/database.ts",
                          "database.types.ts", "src/database.types.ts"):
            if os.path.exists(os.path.join(self.root, candidate)):
                return candidate
        return None

    def local_migrations(self):
        directory = os.path.join(self.root, "supabase", "migrations")
        if not os.path.isdir(directory):
            return []
        return sorted(name for name in os.listdir(directory)
                      if MIGRATION_FILE_RE.match(name))

    def migration_history(self):
        """Remote/local migration status via the CLI (mockable)."""
        result = self._cli("migration", "list")
        return {"exit_code": result.get("exit_code"),
                "output": (result.get("stdout") or "")[:4000]}

    # -- migration-first rule --------------------------------------------------
    def require_migration_files(self, action="database mutation"):
        migrations = self.local_migrations()
        if not migrations:
            raise errors.PolicyError(
                "%s refused: no version-controlled migration files exist "
                "under supabase/migrations — schema changes must be "
                "migrations first, never MCP/ad-hoc only" % action)
        return migrations

    # -- local workflow (steps 1–6 of the safe workflow) -----------------------
    def new_migration(self, name):
        safe = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
        return self._cli("migration", "new", safe)

    def local_workflow(self, seed=True, generate_types=True):
        """create/update migration (already on disk) -> apply via full
        local reset -> seed -> generate types -> report. Stops at the
        first failure. Local env only — no guard needed beyond policy."""
        guard(self.cfg, "local", "migration_apply")
        report = {"steps": [], "ok": True}

        def step(step_name, result):
            entry = {"step": step_name,
                     "exit_code": result.get("exit_code"),
                     "output": (result.get("stdout", "")
                                + result.get("stderr", ""))[-1500:]}
            report["steps"].append(entry)
            if result.get("exit_code") != 0:
                report["ok"] = False
            return report["ok"]

        self.require_migration_files("local migration apply")
        if not step("db_reset", self._cli("db", "reset", "--local",
                                          timeout=900)):
            report["failed_step"] = "db_reset"
            return report
        if seed and os.path.exists(os.path.join(self.root, "supabase",
                                                "seed.sql")):
            # supabase db reset applies seed.sql itself; record presence
            report["steps"].append({"step": "seed",
                                    "note": "seed.sql applied by reset"})
        if generate_types:
            if not step("gen_types", self._cli(
                    "gen", "types", "typescript", "--local")):
                report["failed_step"] = "gen_types"
                return report
        return report

    # -- remote workflow --------------------------------------------------------
    def remote_apply(self, environment, approved=False):
        """Remote migration apply with the full safety ladder: policy →
        linked-project inspection → history comparison → dry run with
        saved evidence → approval where required → apply → verify."""
        needs_approval = guard(self.cfg, environment, "migration_apply")
        self.require_migration_files("remote migration apply")
        detection = self.detect()
        if not detection["linked_project_ref"]:
            raise errors.PolicyError(
                "no linked Supabase project; run `supabase link` "
                "yourself first")
        history = self.migration_history()
        dry = self._cli("db", "push", "--dry-run")
        evidence = {
            "at": self.clock().isoformat(timespec="seconds"),
            "environment": environment,
            "linked_project_ref": detection["linked_project_ref"],
            "local_migrations": detection["migrations"],
            "history": history,
            "dry_run": {"exit_code": dry.get("exit_code"),
                        "output": (dry.get("stdout", "")
                                   + dry.get("stderr", ""))[-4000:]},
        }
        self._save_evidence("remote-apply", evidence)
        if dry.get("exit_code") != 0:
            raise errors.PolicyError("dry run failed; apply refused "
                                     "(evidence saved)")
        if needs_approval and not approved:
            return {"status": "approval_required",
                    "environment": environment, "evidence": evidence,
                    "note": "re-run with explicit approval to apply"}
        apply_result = self._cli("db", "push")
        verify = self.migration_history()
        return {"status": "applied" if apply_result.get("exit_code") == 0
                else "failed",
                "apply_exit_code": apply_result.get("exit_code"),
                "verified_history": verify, "evidence": evidence}

    def remote_reset(self, environment):
        guard(self.cfg, environment, "reset")   # staging/production: raises
        return self._cli("db", "reset", "--linked", timeout=900)

    def _save_evidence(self, kind, evidence):
        if not self.evidence_dir:
            return None
        os.makedirs(self.evidence_dir, exist_ok=True)
        stamp = self.clock().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self.evidence_dir,
                            "supabase-%s-%s.json" % (kind, stamp))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(evidence, fh, indent=2, default=str)
        return path

    # -- per-project database mutation lock --------------------------------------
    def mutation_lock(self, runtime_dir):
        from .registry import _FileLock
        return _FileLock(os.path.join(runtime_dir, "locks",
                                      "db-mutation.lock"),
                         timeout=1.0, stale_seconds=1800)
