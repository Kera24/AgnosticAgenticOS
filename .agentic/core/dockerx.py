"""Restricted Docker adapter: project-scoped Compose operations only.

Every registered project gets a fixed Compose project name
(`agentic-<project-id>`); every operation is an allowlisted argv (never a
shell) forced onto that name, so one project can never touch another's
containers — and nothing here can touch the host at large.

Denied by default (non-exhaustive, enforced by op allowlist + argument
screening): system prune, global volume/container removal, privileged
containers, host-root or docker-socket mounts, publishing dev services on
non-loopback interfaces, and any bare `docker` verb outside the compose
allowlist.
"""
import os

from . import errors, execpolicy

SAFE_OPS = ("config", "build", "up", "down", "ps", "logs", "exec")

FORBIDDEN_TOKENS = (
    "prune", "system", "--privileged", "docker.sock", "--pid=host",
    "--network=host", "--publish-all",
)
FORBIDDEN_MOUNT_PREFIXES = ("/:", "c:\\:", "c:/:")


class DockerAdapter:
    def __init__(self, cfg, project_id, root, runner=None, home=None):
        self.cfg = cfg
        self.project_id = project_id
        self.compose_project = "agentic-%s" % project_id
        self.root = str(root)
        self.runner = runner or self._default_runner
        self.home = home
        docker_cfg = cfg.get("docker") or {}
        self.approved_exec_services = list(
            docker_cfg.get("approved_exec_services") or [])
        self.allowed_ops = [op for op in
                            (docker_cfg.get("allowed_operations")
                             or list(SAFE_OPS)) if op in SAFE_OPS]

    @staticmethod
    def _default_runner(argv, cwd=None, timeout=600, stdin_text=None):
        return execpolicy.run_command(argv, cwd=cwd or ".",
                                      timeout=timeout, source="config",
                                      stdin_text=stdin_text)

    def compose_file(self):
        for name in ("docker-compose.yml", "docker-compose.yaml",
                     "compose.yaml", "compose.yml"):
            path = os.path.join(self.root, name)
            if os.path.exists(path):
                return path
        return None

    def _screen(self, extra_args):
        if "-P" in [str(a) for a in extra_args]:   # publish-all (case-
            raise errors.PolicyError(              # sensitive: -p differs)
                "docker argument '-P' (publish all) is denied by policy")
        joined = " ".join(str(a) for a in extra_args).lower()
        for token in FORBIDDEN_TOKENS:
            if token.lower() in joined.split() or token.lower() in joined:
                raise errors.PolicyError(
                    "docker argument %r is denied by policy" % token)
        if "0.0.0.0" in joined:
            raise errors.PolicyError(
                "publishing on 0.0.0.0 is denied; bind 127.0.0.1")
        for arg in extra_args:
            low = str(arg).lower()
            if low.startswith(("-v", "--volume", "--mount")) or ":" in low:
                for prefix in FORBIDDEN_MOUNT_PREFIXES:
                    if prefix in low:
                        raise errors.PolicyError(
                            "host-root mount is denied by policy")
                if "docker.sock" in low:
                    raise errors.PolicyError(
                        "docker socket mount is denied by policy")
            if (low.startswith("-p") or low.startswith("--publish")) and \
                    "0.0.0.0" in low:
                raise errors.PolicyError(
                    "publishing on 0.0.0.0 is denied; bind 127.0.0.1")

    def compose(self, op, *extra_args, timeout=600):
        """Run one allowlisted compose operation, always scoped to this
        project's compose name."""
        if op not in self.allowed_ops:
            raise errors.PolicyError(
                "docker compose operation %r is not allowed (allowed: %s)"
                % (op, ", ".join(self.allowed_ops)))
        extra = [str(a) for a in extra_args]
        self._screen(extra)
        if op == "exec":
            if not extra or extra[0] not in self.approved_exec_services:
                raise errors.PolicyError(
                    "compose exec is limited to approved services (%s)"
                    % (", ".join(self.approved_exec_services) or "none"))
        compose_file = self.compose_file()
        if compose_file is None:
            raise errors.PolicyError("no compose file found in %s"
                                     % self.root)
        argv = ["docker", "compose", "--project-name",
                self.compose_project, "-f", compose_file, op]
        if op == "up":
            argv += ["-d"]
        if op == "down":
            # down is inherently scoped by --project-name; volumes of other
            # projects are untouchable, and global flags were screened out
            pass
        argv += extra
        return self.runner(argv, cwd=self.root, timeout=timeout)

    def status(self):
        try:
            result = self.compose("ps", "--format", "json", timeout=60)
            return {"available": result.get("exit_code") == 0,
                    "compose_project": self.compose_project,
                    "output": (result.get("stdout") or "")[:2000]}
        except errors.PolicyError as exc:
            return {"available": False, "detail": exc.detail,
                    "compose_project": self.compose_project}

    def build_lock(self):
        """Cross-process docker-build mutex (one build machine-wide)."""
        from .registry import _FileLock
        base = self.home or os.path.join(os.path.expanduser("~"),
                                         ".agentic-os")
        return _FileLock(os.path.join(base, "locks", "docker-build.lock"),
                         timeout=1.0, stale_seconds=3600)

    def build(self, *extra_args, timeout=1800):
        with self.build_lock():
            return self.compose("build", *extra_args, timeout=timeout)
