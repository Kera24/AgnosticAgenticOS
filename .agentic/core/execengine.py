"""Execution-engine abstraction (Phase 4).

A pluggable interface for WHERE/HOW a worker's edits actually get
produced for a task worktree. AgenticOS itself remains authoritative
for everything else -- task selection, the task contract, allowed_paths,
model selection, capacity, deterministic verification, review,
completion, memory, cooling, and recovery are ALL untouched by which
engine is selected here (nothing in this module ever runs the
deterministic gate, QA, or security review -- that happens exactly as
before, on whatever the engine produced). An engine is responsible ONLY
for: attaching to (or creating) the task worktree, launching the coding
agent, tracking its session, translating its events into the platform's
own shape, and collecting the resulting edits.

Two engines:
  - "native" (default, always available): a thin wrapper around the
    platform's own existing coder-invocation path
    (core.project._invoke_coder) -- NOT a reimplementation. Its
    `launch_agent` result is byte-identical to what `_invoke_coder`
    already returned before this module existed.
  - "orca" (optional, disabled by default): supervises an external Orca
    process for the SAME role. Never copies or forks Orca source; talks
    to it only through its own CLI, and only via versioned, documented
    conventions (an explicit --version output and a --json result
    payload) -- anything undocumented or unversioned is treated as
    "unavailable", never trusted blindly. If Orca is absent, disabled,
    or an unsupported version, `select_engine` falls back to native
    automatically -- callers never special-case this themselves.
"""
import datetime as _dt
import json
import os
import re
import shutil
import uuid

from . import taskspace

ENGINE_NATIVE = "native"
ENGINE_ORCA = "orca"

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_TIMEOUT = "timeout"


class EngineError(Exception):
    pass


class EngineUnavailable(EngineError):
    """Raised by an engine's own probe/launch when it cannot run at all.
    Callers (specifically `select_engine`) treat this as "fall back to
    native" -- it is never surfaced to the cycle as a task failure."""


class SessionRequest:
    __slots__ = ("run_id", "task_id", "worktree", "role", "coder_input",
                "chain", "timeout_seconds")

    def __init__(self, run_id, task_id, worktree, role, coder_input,
                chain, timeout_seconds=None):
        self.run_id = run_id
        self.task_id = task_id
        self.worktree = worktree
        self.role = role
        self.coder_input = coder_input
        self.chain = chain
        self.timeout_seconds = timeout_seconds


class SessionResult:
    """Wraps the engine's outcome. `raw` is the full legacy-shaped result
    dict every existing downstream call site already knows how to read
    (`ok`, `edits`, `blocked`, `blocker`, `backend`, `usage`, `error`,
    `capacity`, ...) -- `to_legacy_dict()` returns it unchanged, so
    swapping an engine in never requires the caller to change."""

    def __init__(self, session_id, engine, raw, status, worktree=None,
                events=None):
        self.session_id = session_id
        self.engine = engine
        self.raw = raw
        self.status = status
        self.worktree = worktree
        self.events = events or []

    def to_legacy_dict(self):
        return self.raw


class ExecutionEngine:
    """The interface every engine implements. This codebase doesn't use
    ABCs elsewhere, so this documents the contract via
    NotImplementedError rather than enforcing it structurally."""
    name = "base"

    def installation_detected(self):
        raise NotImplementedError

    def detect_version(self):
        raise NotImplementedError

    def version_supported(self, version):
        raise NotImplementedError

    def probe_capabilities(self):
        raise NotImplementedError

    def smoke_test(self, workdir):
        raise NotImplementedError

    def attach_worktree(self, root, agentic_dir, task_id, base_branch):
        raise NotImplementedError

    def launch_agent(self, request):
        raise NotImplementedError

    def poll_status(self, session_id):
        raise NotImplementedError

    def cancel(self, session_id):
        raise NotImplementedError

    def collect_diff(self, session_id):
        raise NotImplementedError

    def collect_result(self, session_id):
        raise NotImplementedError

    def cleanup(self, session_id):
        raise NotImplementedError


# -- native ---------------------------------------------------------------------

class NativeExecutionEngine(ExecutionEngine):
    name = ENGINE_NATIVE

    def __init__(self, cfg, caller):
        self.cfg = cfg
        self.caller = caller
        self._sessions = {}

    def installation_detected(self):
        return True   # it IS the platform; always available

    def detect_version(self):
        return "native"

    def version_supported(self, version):
        return True

    def probe_capabilities(self):
        return {"available": True, "engine": ENGINE_NATIVE,
                "session_polling": False, "always_available": True}

    def smoke_test(self, workdir):
        return os.path.isdir(workdir)

    def attach_worktree(self, root, agentic_dir, task_id, base_branch):
        return taskspace.create_task_worktree(root, agentic_dir, task_id,
                                              base_branch)

    def launch_agent(self, request):
        from .project import _invoke_coder
        result = _invoke_coder(self.cfg, self.caller, request.coder_input,
                               request.worktree, request.chain,
                               role=request.role)
        session_id = "native-" + (request.run_id or uuid.uuid4().hex[:12])
        status = STATUS_COMPLETED if result.get("ok") else STATUS_FAILED
        session = SessionResult(
            session_id, self.name, result, status,
            worktree=request.worktree,
            events=[{"type": "native_invoke_completed",
                    "ok": result.get("ok"), "blocked": result.get("blocked")}])
        self._sessions[session_id] = session
        return session

    def poll_status(self, session_id):
        session = self._sessions.get(session_id)
        return session.status if session else None

    def cancel(self, session_id):
        return False   # synchronous: already completed by the time
                       # launch_agent returned -- nothing to cancel

    def collect_diff(self, session_id):
        from . import gitops
        session = self._sessions.get(session_id)
        if session is None or not session.worktree:
            return None
        return gitops.diff_text(session.worktree)

    def collect_result(self, session_id):
        return self._sessions.get(session_id)

    def cleanup(self, session_id):
        self._sessions.pop(session_id, None)


# -- orca ---------------------------------------------------------------------

def _default_runner(argv, cwd=None, timeout=30, **kw):
    # supervised, same as every other CLI backend runner (core.supervisor)
    # -- never the bare subprocess.run(timeout=...) this used before,
    # which cannot enforce its own deadline once the child spawns a
    # descendant holding its stdout/stderr pipe handles open.
    from . import supervisor
    return supervisor.default_cli_runner(argv, cwd=cwd, timeout=timeout, **kw)


_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


class OrcaExecutionEngine(ExecutionEngine):
    """Talks to an external `orca` executable via subprocess only --
    never imports it, never reads its internal state/database files,
    never parses an undocumented output format. `--version` and a
    `--json` result payload are the only two conventions this adapter
    depends on; anything else about a real Orca release is unknown to
    this codebase by design (see module docstring)."""
    name = ENGINE_ORCA

    def __init__(self, cfg, executable=None, supported_versions=None,
                runner=None):
        self.cfg = cfg
        ocfg = (cfg.get("orca") or {})
        self.executable = executable or ocfg.get("executable", "orca")
        # explicit only -- an empty list means NOTHING is supported yet
        # (an admin must pin real tested version(s) before this engine
        # can ever be selected), never "assume the latest works".
        self.supported_versions = list(
            supported_versions if supported_versions is not None
            else (ocfg.get("supported_versions") or []))
        self.runner = runner or _default_runner
        self._sessions = {}

    def installation_detected(self):
        if shutil.which(self.executable):
            return True
        # allow an explicit absolute path too (not just PATH lookup)
        return os.path.isfile(self.executable) and os.access(
            self.executable, os.X_OK)

    def detect_version(self):
        if not self.installation_detected():
            return None
        try:
            result = self.runner([self.executable, "--version"], timeout=15)
        except Exception:   # noqa: BLE001 -- any failure means "unknown"
            return None
        if result["exit_code"] != 0:
            return None
        match = _VERSION_RE.search(result["stdout"] + result["stderr"])
        return match.group(1) if match else None

    def version_supported(self, version):
        if not version or not self.supported_versions:
            return False
        return version in self.supported_versions

    def probe_capabilities(self):
        if not self.installation_detected():
            return {"available": False, "installed": False,
                    "reason": "executable not found: %s" % self.executable}
        version = self.detect_version()
        supported = self.version_supported(version)
        return {"available": supported, "installed": True,
                "version": version,
                "supported_versions": list(self.supported_versions),
                "executable": self.executable,
                "reason": None if supported else
                ("version %r not in supported_versions %r"
                 % (version, self.supported_versions))}

    def smoke_test(self, workdir):
        """Non-destructive: a version probe only -- never launches a
        real session, never writes anything to `workdir`."""
        return bool(self.probe_capabilities().get("available"))

    def attach_worktree(self, root, agentic_dir, task_id, base_branch):
        # Orca supervises its own session concept, but the platform's
        # task-isolation guarantees (one worktree per task, stable
        # branch naming, ownership claims, preserved-worktree recovery)
        # are never delegated to it -- both engines share this exact
        # primitive so their worktree isolation is equivalent by
        # construction, not by coincidence.
        return taskspace.create_task_worktree(root, agentic_dir, task_id,
                                              base_branch)

    def launch_agent(self, request):
        caps = self.probe_capabilities()
        if not caps.get("available"):
            raise EngineUnavailable(caps.get("reason")
                                    or "orca unavailable")
        session_id = "orca-" + uuid.uuid4().hex[:16]
        argv = [self.executable, "run", "--json",
               "--workdir", request.worktree, "--role", request.role]
        try:
            raw = self.runner(argv, cwd=request.worktree,
                              timeout=request.timeout_seconds or 900,
                              role=request.role, backend=self.name)
        except Exception as exc:   # noqa: BLE001
            raise EngineUnavailable("orca invocation failed: %s" % exc)
        if raw.get("timed_out"):
            session = SessionResult(
                session_id, self.name,
                _legacy_dict(ok=False, backend="orca",
                            error={"kind": "execution_timeout",
                                  "detail": "orca run timed out"}),
                STATUS_TIMEOUT, worktree=request.worktree)
            self._sessions[session_id] = session
            return session
        events = self._translate_events(raw)
        parsed = self._parse_result(raw)
        ok = raw["exit_code"] == 0 and not parsed.get("blocked")
        legacy = _legacy_dict(
            ok=ok, backend="orca", edits=parsed.get("edits"),
            blocked=parsed.get("blocked", False),
            blocker=parsed.get("blocker"), usage=parsed.get("usage"),
            error=None if ok else {"kind": "model_output_invalid",
                                   "detail": (raw.get("stderr") or "")[:300]})
        session = SessionResult(
            session_id, self.name, legacy,
            STATUS_COMPLETED if ok else STATUS_FAILED,
            worktree=request.worktree, events=events)
        self._sessions[session_id] = session
        return session

    def _translate_events(self, raw_result):
        """Only ever looks for a `--json` stdout payload with a
        recognised, versioned `{"events": [...]}` shape; anything else
        becomes a single opaque `raw_output` event rather than being
        silently parsed/guessed at."""
        try:
            payload = json.loads(raw_result["stdout"])
        except (ValueError, KeyError, TypeError):
            return [{"type": "raw_output",
                    "detail": (raw_result.get("stdout") or "")[:500]}]
        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list):
            return [{"type": "raw_output", "detail": str(payload)[:500]}]
        return [{"type": e.get("type", "unknown"), "detail": e.get("detail")}
               for e in events if isinstance(e, dict)]

    def _parse_result(self, raw_result):
        try:
            payload = json.loads(raw_result["stdout"])
        except (ValueError, KeyError, TypeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {"edits": payload.get("edits"),
                "blocked": bool(payload.get("blocked", False)),
                "blocker": payload.get("blocker"),
                "usage": payload.get("usage")}

    def poll_status(self, session_id):
        session = self._sessions.get(session_id)
        return session.status if session else None

    def cancel(self, session_id):
        # sessions launched by THIS adapter are synchronous
        # (subprocess.run already returned by the time launch_agent
        # gives back a result) -- nothing to cancel mid-flight in this
        # design; always safe/non-destructive to call regardless.
        return False

    def collect_diff(self, session_id):
        from . import gitops
        session = self._sessions.get(session_id)
        if session is None or not session.worktree:
            return None
        return gitops.diff_text(session.worktree)

    def collect_result(self, session_id):
        return self._sessions.get(session_id)

    def cleanup(self, session_id):
        self._sessions.pop(session_id, None)


def _legacy_dict(ok, backend, edits=None, blocked=False, blocker=None,
                 usage=None, error=None):
    """The exact dict shape every existing downstream call site
    (project.py's coder loop) already knows how to read -- see
    `_invoke_coder`'s return value."""
    return {"ok": ok, "backend": backend, "backend_type": "orca",
           "model": None, "role": None, "provider": "orca",
           "content": "", "structured_output": {},
           "edits": edits, "blocked": bool(blocked), "blocker": blocker,
           "usage": usage or {"input_tokens": 0, "output_tokens": 0,
                              "cached_tokens": 0, "estimated": True},
           "capacity": {"remaining_reported": None, "reset_at": None,
                       "retry_after_seconds": None},
           "finish_reason": "completed" if ok else "error",
           "refusal": False, "exit_code": 0 if ok else 1,
           "estimated_cost_usd": 0.0, "error": error}


# -- selection ------------------------------------------------------------------

def select_engine(cfg, caller=None, runner=None):
    """The single decision point for which engine handles the next agent
    invocation. Unconditional, silent-safe fallback: an
    unavailable/incompatible/disabled Orca NEVER blocks a cycle -- native
    runs exactly as if `orca.enabled` were false. Returns
    (engine, decision) where `decision` records why, for the run log."""
    execution_cfg = cfg.get("execution") or {}
    orca_cfg = cfg.get("orca") or {}
    preferred = execution_cfg.get("preferred_engine", ENGINE_NATIVE)
    fallback_to_native = bool(orca_cfg.get("fallback_to_native", True))
    native = NativeExecutionEngine(cfg, caller)
    if preferred != ENGINE_ORCA or not orca_cfg.get("enabled", False):
        return native, {"selected": ENGINE_NATIVE,
                        "reason": "orca not preferred/enabled"}
    orca = OrcaExecutionEngine(cfg, runner=runner)
    caps = orca.probe_capabilities()
    if caps.get("available"):
        return orca, {"selected": ENGINE_ORCA,
                      "reason": "orca available and version-compatible",
                      "capabilities": caps}
    if fallback_to_native:
        return native, {"selected": ENGINE_NATIVE,
                        "reason": "orca unavailable/incompatible (%s); "
                                 "falling back to native"
                                 % caps.get("reason"),
                        "capabilities": caps}
    raise EngineUnavailable(
        "orca unavailable and execution.orca.fallback_to_native is "
        "disabled: %s" % caps.get("reason"))
