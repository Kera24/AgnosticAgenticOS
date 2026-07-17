"""`cce` adapter for the external Code Context Engine.

Security and honesty rules (ADR 0002):
- the engine is never vendored or imported; only an installed CLI is used;
- detection + version validation happen before any use;
- invocation is argv-only through execpolicy (never a shell);
- all returned paths must stay inside the project workspace or the result
  is discarded;
- excluded/secret paths are passed as exclusions AND filtered from results;
- timeouts and malformed output degrade to typed failures, never crashes;
- indexing sends file PATHS to a local process only — if the engine is
  configured with any non-local endpoint the adapter refuses to run.
"""
import json
import os
import shutil

from .. import execpolicy
from .base import (CodeIntelligenceAdapter, DEFAULT_EXCLUDES,
                   clamp_results_to_budget, is_excluded, norm_rel)

SUPPORTED_MAJOR_VERSIONS = (0, 1)   # validated against `cce --version`
DEFAULT_TIMEOUT = 120


class CCEUnavailable(Exception):
    pass


class CCEAdapter(CodeIntelligenceAdapter):
    provider_name = "cce"

    def __init__(self, project_root, memory_dir, cfg=None, runner=None,
                 which=None):
        super().__init__(project_root, memory_dir, cfg)
        self.binary = (cfg or {}).get("cce_binary", "cce")
        self.runner = runner or self._default_runner
        self.which = which or shutil.which
        endpoint = (cfg or {}).get("cce_endpoint")
        if endpoint and not any(h in str(endpoint)
                                for h in ("127.0.0.1", "localhost")):
            raise CCEUnavailable("non-local CCE endpoint refused: code is "
                                 "never sent off this machine for indexing")

    @staticmethod
    def _default_runner(argv, cwd=None, timeout=DEFAULT_TIMEOUT,
                        stdin_text=None):
        return execpolicy.run_command(argv, cwd=cwd or ".", timeout=timeout,
                                      source="config", stdin_text=stdin_text)

    # -- detection -----------------------------------------------------------
    def detect(self):
        path = self.which(self.binary)
        if not path:
            return {"installed": False, "version": None}
        run = self.runner([self.binary, "--version"], timeout=30)
        raw = (run["stdout"] or run["stderr"]).strip()
        version = _parse_version(raw)
        supported = version is not None and \
            version[0] in SUPPORTED_MAJOR_VERSIONS
        return {"installed": True, "version": raw[:60],
                "parsed_version": version, "supported": supported}

    def _require(self):
        info = self.detect()
        if not info["installed"]:
            raise CCEUnavailable("cce binary %r not found" % self.binary)
        if not info.get("supported"):
            raise CCEUnavailable("cce version %r unsupported (majors %s)"
                                 % (info.get("version"),
                                    list(SUPPORTED_MAJOR_VERSIONS)))

    # -- structured invocation --------------------------------------------------
    def _invoke(self, args, timeout=DEFAULT_TIMEOUT):
        argv = [self.binary] + list(args) + ["--json"]
        run = self.runner(argv, cwd=self.project_root, timeout=timeout)
        if run.get("timed_out"):
            raise CCEUnavailable("cce timed out after %ss" % timeout)
        if run.get("exit_code") != 0:
            raise CCEUnavailable("cce failed (exit %s): %s"
                                 % (run.get("exit_code"),
                                    (run.get("stderr") or "")[:200]))
        try:
            return json.loads(run.get("stdout") or "")
        except ValueError:
            raise CCEUnavailable("cce returned malformed JSON")

    def _exclude_args(self):
        out = []
        for pattern in DEFAULT_EXCLUDES + self.excludes:
            out.extend(["--exclude", pattern])
        return out

    # -- lifecycle ---------------------------------------------------------------
    def initialize(self):
        self._require()
        return {"ok": True, "provider": "cce"}

    def index_full(self):
        self._require()
        data = self._invoke(["index", "--root", self.project_root]
                            + self._exclude_args(), timeout=600)
        files = int(data.get("files_indexed") or 0)
        revision = data.get("revision") or self._git_revision()
        self.save_index_state(revision, files)
        return {"ok": True, "provider": "cce", "files_indexed": files,
                "revision": revision}

    def index_changes(self, changed_paths, revision):
        self._require()
        safe = [p for p in (changed_paths or [])
                if not is_excluded(p, self.excludes)]
        if not safe:
            self.save_index_state(revision, (self.load_index_state() or
                                             {}).get("files_indexed", 0))
            return {"ok": True, "provider": "cce", "files_indexed": 0}
        data = self._invoke(["index", "--root", self.project_root,
                             "--changed"] + [norm_rel(p) for p in safe]
                            + self._exclude_args(), timeout=300)
        self.save_index_state(revision, int(data.get("files_indexed") or 0))
        return {"ok": True, "provider": "cce",
                "files_indexed": int(data.get("files_indexed") or 0)}

    def status(self):
        state = self.load_index_state()
        try:
            info = self.detect()
        except Exception:
            info = {"installed": False}
        return {"provider": "cce", "installed": info.get("installed", False),
                "version": info.get("version"),
                "supported": info.get("supported"),
                "indexed": state is not None,
                "revision": (state or {}).get("revision"),
                "stale": self.stale(self._git_revision()),
                "files_indexed": (state or {}).get("files_indexed"),
                "indexed_at": (state or {}).get("indexed_at")}

    def health_check(self):
        try:
            self._require()
            return {"ok": True, "provider": "cce"}
        except CCEUnavailable as exc:
            return {"ok": False, "provider": "cce", "detail": str(exc)}

    def remove_project(self):
        self._require()
        self._invoke(["remove", "--root", self.project_root])
        return {"ok": True}

    # -- retrieval -----------------------------------------------------------------
    def search(self, query, paths=None, languages=None, limit=12,
               token_budget=None):
        self._require()
        args = ["search", "--root", self.project_root,
                "--query", str(query), "--limit", str(int(limit))]
        for p in paths or []:
            args.extend(["--path", norm_rel(p)])
        for lang in languages or []:
            args.extend(["--language", str(lang)])
        data = self._invoke(args)
        return clamp_results_to_budget(
            self._sanitize(data.get("results") or []), token_budget)

    def expand(self, result_ids, token_budget=None):
        self._require()
        data = self._invoke(["expand"] + [str(r) for r in result_ids or []]
                            + ["--root", self.project_root])
        return clamp_results_to_budget(
            self._sanitize(data.get("results") or []), token_budget)

    def related(self, symbol_or_result_id, depth=1, token_budget=None):
        self._require()
        data = self._invoke(["related", str(symbol_or_result_id),
                             "--depth", str(int(depth)),
                             "--root", self.project_root])
        return clamp_results_to_budget(
            self._sanitize(data.get("results") or []), token_budget)

    # -- result hygiene ---------------------------------------------------------
    def _sanitize(self, raw_results):
        """Drop results outside the workspace, excluded paths, or missing
        mandatory fields. Malformed entries are skipped, never fatal."""
        root = os.path.realpath(self.project_root)
        out = []
        for entry in raw_results:
            if not isinstance(entry, dict):
                continue
            rel = norm_rel(str(entry.get("path") or ""))
            if not rel or is_excluded(rel, self.excludes):
                continue
            full = os.path.realpath(os.path.join(root, rel))
            if not (full == root or full.startswith(root + os.sep)):
                continue   # path escape: discard
            try:
                start = int(entry.get("start_line") or 1)
                end = int(entry.get("end_line") or start)
            except (TypeError, ValueError):
                continue
            out.append({
                "id": str(entry.get("id") or "cce:%s:%d-%d"
                          % (rel, start, end)),
                "path": rel, "start_line": start, "end_line": end,
                "snippet": str(entry.get("snippet") or "")[:20000],
                "score": float(entry.get("score") or 0.0),
                "language": str(entry.get("language") or ""),
                "provider": "cce"})
        return out

    def _git_revision(self):
        from .. import gitops
        try:
            return gitops.run_git(["rev-parse", "HEAD"],
                                  cwd=self.project_root,
                                  check=False).strip() or None
        except Exception:
            return None


def _parse_version(raw):
    import re
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", raw or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
