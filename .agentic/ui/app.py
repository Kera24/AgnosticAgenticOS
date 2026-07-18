"""FastAPI application for the Agentic OS Control Centre.

Security model (loopback-only control plane for a code-execution system):
- binds to 127.0.0.1 only (enforced again in serve.py);
- every request must carry a loopback Host header;
- state-changing requests with an Origin header must match an allowed
  loopback origin (CSRF defence; non-browser local clients send no Origin);
- no arbitrary command endpoint, no arbitrary filesystem endpoint — every
  path is validated and confined to known roots;
- no push/merge/deploy endpoints exist;
- every state-changing dashboard action is written to the audit trail.
"""
import datetime as _dt
import os
import queue as _queue
import threading
import time

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from core import config as config_mod
from core import logs as logs_mod
from ui import snapshots
from ui import settings as settings_mod
from ui.bus import EventBus, sse_format
from ui.ops import OperationConflict, OperationManager
from ui.watch import StateWatcher

API = "/api/v1"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
DETECTION_TTL_SECONDS = 120
UI_VERSION = "1.0.0"


class ProjectStartBody(BaseModel):
    plan_text: str | None = Field(default=None, max_length=1_000_000)
    plan_path: str | None = Field(default=None, max_length=1000)


class ConfirmBody(BaseModel):
    confirm: bool = False


def _loopback_origin(origin):
    try:
        scheme, rest = origin.split("://", 1)
        host = rest.split("/", 1)[0].rsplit(":", 1)[0]
        return scheme == "http" and host.lower().strip("[]") in \
            {"127.0.0.1", "localhost", "::1"}
    except (ValueError, AttributeError):
        return False


def create_app(load_cfg=None, detector=None, static_dir=None,
               allow_dev_origin=False):
    """Build the app. `load_cfg`/`detector` are injectable for tests so no
    real CLI is ever probed or invoked during testing."""
    load_cfg = load_cfg or (lambda: config_mod.load_config())
    bus = EventBus()
    memory = str(config_mod.AGENTIC_DIR / "memory")
    ops = OperationManager(bus, persist_path=os.path.join(
        memory, "ui-operations.json"))
    watcher = StateWatcher(str(config_mod.AGENTIC_DIR), bus)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        watcher.start()
        yield
        watcher.stop()

    app = FastAPI(title="Agentic OS Control Centre", version=UI_VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.bus, app.state.ops, app.state.watcher = bus, ops, watcher
    app.state.detection = {"at": 0, "detected": {}, "apis": {}}
    detection_lock = threading.Lock()

    dev_origins = ["http://127.0.0.1:5173", "http://localhost:5173"] \
        if allow_dev_origin else []
    if dev_origins:
        app.add_middleware(CORSMiddleware, allow_origins=dev_origins,
                           allow_methods=["*"], allow_headers=["*"])

    # -- request guard -------------------------------------------------------
    @app.middleware("http")
    async def loopback_guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if host not in LOOPBACK_HOSTS:
            return JSONResponse({"detail": "loopback access only"},
                                status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin is not None and not _loopback_origin(origin):
                return JSONResponse({"detail": "origin not allowed"},
                                    status_code=403)
        return await call_next(request)

    def audit(event, **fields):
        logs_mod.decision(memory, dict(fields, event=event,
                                       source="dashboard"))

    def cfg():
        try:
            return load_cfg()
        except Exception as exc:
            raise HTTPException(500, "configuration failed to load: %s"
                                % exc)

    def run_detection(force=False):
        with detection_lock:
            cache = app.state.detection
            if not force and time.time() - cache["at"] < \
                    DETECTION_TTL_SECONDS:
                return cache["detected"], cache["apis"]
            if detector is not None:
                detected, apis = detector(cfg())
            else:
                from core.setupwiz import detect_backends
                try:
                    detected, apis = detect_backends(cfg())
                except Exception:
                    detected, apis = {}, {}
            app.state.detection = {"at": time.time(), "detected": detected,
                                   "apis": apis}
            return detected, apis

    # -- health / doctor -----------------------------------------------------
    @app.get(API + "/health")
    def health():
        return {"ok": True, "version": UI_VERSION, "time": snapshots.now_iso()}

    @app.get(API + "/doctor")
    def doctor():
        from core.doctor import run_doctor
        ok, checks = run_doctor(cfg=cfg())
        return {"ok": ok, "checks": [{"level": lv, "message": msg}
                                     for lv, msg in checks]}

    # -- project -----------------------------------------------------------------
    @app.get(API + "/project")
    def project():
        return snapshots.project_snapshot(cfg())

    @app.get(API + "/project/plan")
    def project_plan():
        return snapshots.plan_documents(cfg())

    @app.get(API + "/project/backlog")
    def project_backlog():
        return {"tasks": snapshots.backlog(cfg())}

    @app.get(API + "/project/milestones")
    def project_milestones():
        snap = snapshots.project_snapshot(cfg())
        return {"milestones": snap["milestones"],
                "progress": snap["progress"]}

    @app.get(API + "/project/blockers")
    def project_blockers():
        snap = snapshots.project_snapshot(cfg())
        return {"blockers": snap["blockers"],
                "human_blockers": snap["human_blockers"]}

    @app.get(API + "/project/activity")
    def project_activity(limit: int = 300):
        limit = max(1, min(int(limit), 1000))
        return {"entries": snapshots.activity_entries(limit=limit)}

    def _resolve_plan(body: ProjectStartBody, configuration):
        provided = [p for p in (body.plan_text, body.plan_path)
                    if p is not None]
        if len(provided) != 1:
            raise HTTPException(422, "provide exactly one of plan_text or "
                                     "plan_path")
        if body.plan_text is not None:
            text = body.plan_text.strip()
            if len(text) < 40:
                raise HTTPException(422, "plan is too short to architect a "
                                         "project (minimum 40 characters)")
            return text, "(pasted plan)"
        root = os.path.realpath(str(config_mod.repo_root(configuration)))
        raw = body.plan_path.strip().strip('"')
        candidate = raw if os.path.isabs(raw) else os.path.join(root, raw)
        real = os.path.realpath(candidate)
        if not (real == root or real.startswith(root + os.sep)):
            raise HTTPException(422, "plan_path must be inside the "
                                     "repository root")
        base = os.path.basename(real).lower()
        if not base.endswith((".md", ".markdown", ".txt")):
            raise HTTPException(422, "plan must be a .md, .markdown or .txt "
                                     "file")
        if base.startswith(".env") or os.sep + ".git" + os.sep in real:
            raise HTTPException(422, "plan_path is not allowed")
        if not os.path.isfile(real):
            raise HTTPException(404, "plan file not found")
        if os.path.getsize(real) > 1_000_000:
            raise HTTPException(422, "plan file exceeds 1 MB")
        with open(real, encoding="utf-8", errors="replace") as fh:
            return fh.read(), real

    @app.post(API + "/project/plan/preview")
    def plan_preview(body: ProjectStartBody):
        text, source = _resolve_plan(body, cfg())
        from core.redact import redact
        return {"source": source, "length": len(text),
                "content": redact(text[:200_000])}

    def _start_operation(kind, runner, detail=None):
        try:
            return ops.start(kind, "project", runner, detail=detail)
        except OperationConflict as exc:
            raise HTTPException(409, {
                "message": "another project operation is running",
                "operation": {k: exc.existing[k] for k in
                              ("id", "kind", "status", "started_at")}})

    @app.post(API + "/project/start")
    def project_start_route(body: ProjectStartBody):
        configuration = cfg()
        from core import projstate
        if projstate.exists(str(config_mod.AGENTIC_DIR)):
            raise HTTPException(409, "a project already exists; the "
                                     "dashboard never deletes project state")
        text, source = _resolve_plan(body, configuration)
        plans_dir = os.path.join(str(config_mod.AGENTIC_DIR), "runs",
                                 "ui-plans")
        os.makedirs(plans_dir, exist_ok=True)
        plan_path = os.path.join(plans_dir, "plan-%s.md"
                                 % _dt.datetime.now().strftime(
                                     "%Y%m%d-%H%M%S"))
        with open(plan_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        audit("ui_project_start", plan_source=str(source),
              plan_bytes=len(text))

        def runner():
            from core.project import project_start
            return project_start(load_cfg(), plan_path)
        return _start_operation("project.start", runner,
                                detail="architecting project")

    @app.post(API + "/project/run")
    def project_run_route():
        audit("ui_project_run")

        def runner():
            from core.project import project_run
            return project_run(load_cfg(), max_cycles=1)
        return _start_operation("project.run", runner,
                                detail="running one cycle")

    @app.post(API + "/project/resume")
    def project_resume_route():
        from core.project import project_resume
        audit("ui_project_resume")
        result = project_resume(cfg())
        bus.publish("state", {"changed": "scheduler"})
        return result

    @app.post(API + "/project/pause")
    def project_pause_route():
        from core.project import project_pause
        audit("ui_project_pause")
        result = project_pause(cfg())
        bus.publish("state", {"changed": "scheduler"})
        return result

    @app.post(API + "/project/review")
    def project_review_route():
        audit("ui_final_audit")

        def runner():
            from core.project import final_audit
            return final_audit(load_cfg())
        return _start_operation("project.review", runner,
                                detail="running final audit")

    # -- agents -------------------------------------------------------------------
    @app.get(API + "/agents")
    def agents():
        return {"agents": snapshots.agents_snapshot(cfg())}

    # -- backends -------------------------------------------------------------------
    @app.get(API + "/backends")
    def backends():
        detected, apis = run_detection()
        return {"backends": snapshots.backends_snapshot(cfg(), detected,
                                                        apis),
                "detected_at": app.state.detection["at"]}

    @app.post(API + "/backends/refresh")
    def backends_refresh():
        audit("ui_backends_refresh")
        detected, apis = run_detection(force=True)
        bus.publish("state", {"changed": "backends"})
        return {"backends": snapshots.backends_snapshot(cfg(), detected,
                                                        apis)}

    def _known_backend(configuration, name):
        if name not in (configuration.get("backends") or {}):
            raise HTTPException(404, "unknown backend %r" % name)

    @app.post(API + "/backends/{name}/smoke-test")
    def backend_smoke(name: str, body: ConfirmBody):
        configuration = cfg()
        _known_backend(configuration, name)
        if not body.confirm:
            raise HTTPException(422, "smoke tests consume real subscription "
                                     "allowance or API cost; set "
                                     "confirm=true to proceed")
        audit("ui_smoke_test", backend=name)

        def runner():
            from core.backends import build_backend
            adapter = build_backend(load_cfg(), name)
            ok = adapter.smoke_test(str(config_mod.repo_root(load_cfg())))
            return {"backend": name, "ok": bool(ok)}
        try:
            return ops.start("backend.smoke-test", "backend", runner,
                             detail=name)
        except OperationConflict as exc:
            raise HTTPException(409, {
                "message": "another backend operation is running",
                "operation": {k: exc.existing[k] for k in
                              ("id", "kind", "status", "started_at")}})

    @app.post(API + "/backends/{name}/reset-breaker")
    def backend_reset_breaker(name: str, body: ConfirmBody):
        configuration = cfg()
        _known_backend(configuration, name)
        if not body.confirm:
            raise HTTPException(422, "resetting a circuit breaker discards "
                                     "observed failure state; set "
                                     "confirm=true to proceed")
        from core.breaker import BreakerBoard
        board = BreakerBoard(memory)
        entry = board.entry(name)
        entry.update(state="available", unavailable_until=None,
                     consecutive_failures=0, failed_since=None)
        board.save()
        audit("ui_breaker_reset", backend=name)
        bus.publish("state", {"changed": "backends"})
        return {"backend": name, "state": "available"}

    # -- capacity / verification -------------------------------------------------
    @app.get(API + "/capacity")
    def capacity():
        return snapshots.capacity_snapshot(cfg())

    @app.get(API + "/verification")
    def verification():
        return snapshots.verification_snapshot(cfg())

    @app.get(API + "/logs/{run}/{name}")
    def run_log(run: str, name: str):
        try:
            return snapshots.read_run_log(run, name)
        except snapshots.LogAccessError as exc:
            raise HTTPException(404, str(exc))

    # -- context / memory / knowledge / skills / routing (Phase 10) -----------
    from ui import intel

    @app.get(API + "/context")
    def context_view():
        return intel.context_snapshot(cfg())

    @app.get(API + "/context/search")
    def context_search(q: str = ""):
        q = q.strip()
        if not q:
            raise HTTPException(422, "query required")
        return intel.context_search(cfg(), q[:500])

    @app.get(API + "/memory")
    def memory_view(q: str = "", include_superseded: bool = False):
        snapshot = intel.memory_snapshot(cfg())
        snapshot.update(intel.memory_search(
            cfg(), q.strip()[:500], include_superseded=include_superseded))
        return snapshot

    @app.get(API + "/memory/{record_id}/timeline")
    def memory_timeline(record_id: str):
        return intel.memory_timeline(cfg(), record_id[:64])

    @app.get(API + "/memory/records")
    def memory_records(ids: str = ""):
        wanted = [i.strip()[:64] for i in ids.split(",") if i.strip()][:20]
        return intel.memory_details(cfg(), wanted)

    class ForgetBody(BaseModel):
        id: str = Field(max_length=64)
        confirm: bool = False

    @app.post(API + "/memory/forget")
    def memory_forget(body: ForgetBody):
        if not body.confirm:
            raise HTTPException(422, "confirmation required to forget a "
                                     "memory record")
        result = intel.memory_forget(cfg(), body.id)
        audit("ui_memory_forget", record=body.id,
              forgotten=result["forgotten"])
        if not result["forgotten"]:
            raise HTTPException(404, "unknown memory record")
        return result

    @app.get(API + "/knowledge")
    def knowledge_view():
        return intel.knowledge_snapshot(cfg())

    @app.get(API + "/knowledge/doc")
    def knowledge_doc(path: str):
        try:
            doc = intel.knowledge_document(cfg(), path[:500])
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if doc is None:
            raise HTTPException(404, "document not found")
        return doc

    @app.get(API + "/skills")
    def skills_view():
        return intel.skills_snapshot(cfg())

    @app.post(API + "/skills/{skill_id}/{action}")
    def skills_action(skill_id: str, action: str, body: ConfirmBody):
        if action not in ("enable", "disable", "verify"):
            raise HTTPException(404, "unknown action")
        if action in ("enable", "disable") and not body.confirm:
            raise HTTPException(422, "confirmation required")
        from core.skillreg import SkillError
        try:
            result = intel.skill_action(cfg(), skill_id[:64], action)
        except SkillError as exc:
            raise HTTPException(422, str(exc))
        audit("ui_skill_action", skill=skill_id[:64], action=action)
        bus.publish("state", {"changed": "skills"})
        return result

    @app.get(API + "/routing")
    def routing_view():
        return intel.routing_snapshot(cfg())

    # -- multi-project portfolio / fleet / mcp / auth (MP Phase 9) -----------
    from ui import portfolio as portfolio_mod
    from core.registry import RegistryError

    class AddProjectBody(BaseModel):
        name: str = Field(max_length=100)
        root: str = Field(max_length=500)
        plan: str | None = Field(default="plan.md", max_length=200)
        create: bool = False

    class ActionBody(BaseModel):
        confirm: bool = False

    DESTRUCTIVE_PROJECT_ACTIONS = {"archive", "remove", "stop"}
    PROJECT_ACTIONS = {"init", "doctor", "pause", "resume", "stop",
                       "enable", "archive", "remove"}

    @app.get(API + "/portfolio")
    def portfolio_view():
        return portfolio_mod.portfolio_snapshot(cfg())

    @app.post(API + "/portfolio/add")
    def portfolio_add(body: AddProjectBody):
        try:
            record = portfolio_mod.add_project(cfg(), body.name, body.root,
                                               plan=body.plan,
                                               create=body.create)
        except RegistryError as exc:
            raise HTTPException(422, str(exc.detail
                                         if hasattr(exc, "detail")
                                         else exc))
        audit("ui_project_add", project=record["id"],
              root=record["root_path"], created=body.create)
        bus.publish("state", {"changed": "portfolio"})
        return record

    @app.post(API + "/portfolio/{project_id}/{action}")
    def portfolio_action(project_id: str, action: str, body: ActionBody):
        if action not in PROJECT_ACTIONS:
            raise HTTPException(404, "unknown action")
        if action in DESTRUCTIVE_PROJECT_ACTIONS and not body.confirm:
            raise HTTPException(422, "confirmation required for %s"
                                % action)
        try:
            result = portfolio_mod.project_action(cfg(), project_id[:64],
                                                  action)
        except RegistryError as exc:
            raise HTTPException(404, str(exc.detail
                                         if hasattr(exc, "detail")
                                         else exc))
        audit("ui_project_action", project=project_id[:64], action=action)
        bus.publish("state", {"changed": "portfolio"})
        return result

    @app.get(API + "/fleet")
    def fleet_view():
        return portfolio_mod.fleet_snapshot(cfg())

    @app.post(API + "/fleet/pause")
    def fleet_pause(body: ActionBody):
        if not body.confirm:
            raise HTTPException(422, "confirmation required")
        from core import fleet as fleet_mod
        from core.registry import ProjectRegistry
        state = fleet_mod.set_global_pause(ProjectRegistry().home, True)
        audit("ui_fleet_pause")
        bus.publish("state", {"changed": "fleet"})
        return state

    @app.post(API + "/fleet/resume")
    def fleet_resume():
        from core import fleet as fleet_mod
        from core.registry import ProjectRegistry
        state = fleet_mod.set_global_pause(ProjectRegistry().home, False)
        audit("ui_fleet_resume")
        bus.publish("state", {"changed": "fleet"})
        return state

    @app.get(API + "/auth")
    def auth_view():
        return portfolio_mod.auth_snapshot(cfg())

    @app.get(API + "/mcp")
    def mcp_view():
        return portfolio_mod.mcp_snapshot(cfg())

    @app.post(API + "/mcp/{server_id}/{action}")
    def mcp_action(server_id: str, action: str, body: ActionBody):
        from core.mcp import MCPError
        if action in ("enable", "disable", "remove") and not body.confirm:
            raise HTTPException(422, "confirmation required")
        try:
            result = portfolio_mod.mcp_action(cfg(), server_id[:64],
                                              action)
        except MCPError as exc:
            raise HTTPException(422, str(exc))
        except ValueError:
            raise HTTPException(404, "unknown action")
        audit("ui_mcp_action", server=server_id[:64], action=action)
        return result

    @app.get(API + "/skills/market")
    def skills_market():
        return portfolio_mod.market_snapshot(cfg())

    @app.post(API + "/skills/market/{skill_id}/{action}")
    def skills_market_action(skill_id: str, action: str,
                             body: ActionBody):
        from core.skillreg import SkillError
        if action in ("approve", "reject", "rollback") and \
                not body.confirm:
            raise HTTPException(422, "confirmation required")
        try:
            result = portfolio_mod.market_action(cfg(), skill_id[:64],
                                                 action)
        except SkillError as exc:
            raise HTTPException(422, str(exc))
        except ValueError:
            raise HTTPException(404, "unknown action")
        audit("ui_skill_market_action", skill=skill_id[:64],
              action=action)
        return result

    # -- settings --------------------------------------------------------------------
    @app.get(API + "/settings")
    def get_settings():
        return settings_mod.effective_settings(cfg())

    @app.put(API + "/settings")
    def put_settings(body: dict = Body(...)):
        try:
            patch = settings_mod.apply_update(cfg(), body)
        except settings_mod.SettingsError as exc:
            raise HTTPException(422, str(exc))
        audit("ui_settings_update", sections=sorted(patch.keys()))
        bus.publish("state", {"changed": "settings"})
        return {"saved": True,
                "settings": settings_mod.effective_settings(cfg())}

    # -- operations --------------------------------------------------------------------
    @app.get(API + "/operations")
    def operations():
        return {"operations": ops.list()}

    @app.get(API + "/operations/{op_id}")
    def operation(op_id: str):
        op = ops.get(op_id)
        if not op:
            raise HTTPException(404, "unknown operation")
        return op

    # -- events (SSE) ------------------------------------------------------------------
    @app.get(API + "/events")
    def events(request: Request):
        last_id = request.headers.get("last-event-id") or \
            request.query_params.get("last_event_id")
        # Optional bounded stream: the browser's EventSource reconnects
        # automatically (with Last-Event-ID), so ending a stream is
        # transparent to clients and keeps tests/CLIs from blocking forever.
        try:
            max_seconds = float(request.query_params.get("max_seconds", 0))
        except ValueError:
            max_seconds = 0
        deadline = time.time() + max_seconds if max_seconds > 0 else None
        client = bus.subscribe(last_event_id=last_id)

        def stream():
            try:
                yield "retry: 3000\n\n"
                while True:
                    wait = 15.0
                    if deadline is not None:
                        wait = min(wait, deadline - time.time())
                        if wait <= 0:
                            return
                    try:
                        event = client.get(timeout=wait)
                        yield sse_format(event)
                    except _queue.Empty:
                        if deadline is None:
                            yield ": keepalive\n\n"
            finally:
                bus.unsubscribe(client)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store",
                                          "X-Accel-Buffering": "no"})

    # -- static frontend -----------------------------------------------------------
    if static_dir and os.path.isdir(static_dir):
        assets = os.path.join(static_dir, "assets")
        if os.path.isdir(assets):
            from fastapi.staticfiles import StaticFiles
            app.mount("/assets", StaticFiles(directory=assets),
                      name="assets")
        index = os.path.join(static_dir, "index.html")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404, "unknown API route")
            candidate = os.path.realpath(os.path.join(static_dir, path))
            root = os.path.realpath(static_dir)
            if path and candidate.startswith(root + os.sep) and \
                    os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(index)

    return app
