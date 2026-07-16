"""Dashboard API: security boundaries, state endpoints, controls,
operations, settings persistence, SSE and no-credential-exposure. No real
CLI or API call ever happens: detection and orchestration are injected."""
import json
import os
import time

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


NO_DETECT = lambda cfg: ({}, {})  # noqa: E731


@pytest.fixture
def ui_client(sandbox, monkeypatch):
    """App bound to the sandboxed .agentic dir; loopback base URL.
    Backend detection is stubbed EVERYWHERE (including inside doctor) so no
    real CLI process is ever spawned by a test."""
    from core import setupwiz
    monkeypatch.setattr(setupwiz, "detect_backends",
                        lambda cfg, **kw: ({}, {}))
    from ui.app import create_app
    cfg = sandbox["cfg"]
    cfg["backends"] = {
        "claude": {"type": "cli", "kind": "configured", "binary": "claude"},
        "codex": {"type": "cli", "kind": "codex", "binary": "codex"},
        "ollama": {"type": "local", "model": None,
                   "api_key_required": False, "cost_free": True},
    }
    cfg["routing"] = {"mode": "simple", "primary": "claude",
                      "fallbacks": ["ollama"]}

    def load_cfg():
        # mirror production layering: machine config overrides the base
        from core.config import AGENTIC_DIR, deep_merge
        machine_path = AGENTIC_DIR / "config.machine.yaml"
        if machine_path.exists():
            with open(machine_path, encoding="utf-8") as fh:
                return deep_merge(cfg, yaml.safe_load(fh) or {})
        return cfg

    app = create_app(load_cfg=load_cfg, detector=NO_DETECT)
    client = TestClient(app, base_url="http://127.0.0.1")
    client.sandbox = sandbox
    return client


def seed_project(sandbox, tasks=None, milestones=None):
    from core import projstate
    a = str(sandbox["agentic"])
    tasks = tasks if tasks is not None else [
        {"id": "T-1", "milestone": "M1", "description": "first task",
         "status": "pending"},
        {"id": "T-2", "milestone": "M1", "description": "second task",
         "status": "done"},
    ]
    projstate.save_backlog(a, [projstate.normalize_task(t) for t in tasks])
    projstate.write_yaml(a, "milestones.yaml", {"milestones": (
        milestones or [{"id": "M1", "title": "Milestone one"}])})
    projstate.write_yaml(a, "blockers.yaml", {"blockers": []})
    projstate.write_text(a, "PROJECT.md", "# Project Plan\n\ntest plan")
    projstate.write_text(a, "architecture.md", "# Architecture\n\ndesign")
    projstate.write_yaml(a, "acceptance-criteria.yaml",
                         {"requirements_map": [],
                          "completion_criteria": ["it works"]})
    projstate.write_yaml(a, "decisions.yaml",
                         {"human_decisions_needed": [], "decided": []})
    projstate.refresh_progress(a)


# -- security boundaries -------------------------------------------------------

def test_non_loopback_host_rejected(ui_client):
    r = ui_client.get("/api/v1/health",
                      headers={"Host": "evil.example.com"})
    assert r.status_code == 403


def test_loopback_host_accepted(ui_client):
    assert ui_client.get("/api/v1/health").status_code == 200


def test_cross_origin_state_change_rejected(ui_client):
    r = ui_client.post("/api/v1/project/pause",
                       headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_loopback_origin_state_change_allowed(ui_client):
    r = ui_client.post("/api/v1/project/pause",
                       headers={"Origin": "http://127.0.0.1:8765"})
    assert r.status_code == 200


def test_no_arbitrary_command_or_push_endpoints(ui_client):
    for path in ("/api/v1/exec", "/api/v1/shell", "/api/v1/push",
                 "/api/v1/merge", "/api/v1/deploy", "/api/v1/git"):
        assert ui_client.post(path).status_code in (403, 404, 405)


def test_log_endpoint_rejects_traversal(ui_client):
    for run, name in (("..", "x"), ("cycle-1", ".."),
                      ("..%2F..%2Fmemory", "decisions"),
                      ("cycle/../../memory", "usage")):
        r = ui_client.get("/api/v1/logs/%s/%s" % (run, name))
        assert r.status_code == 404, (run, name)


def test_log_endpoint_serves_only_run_logs(ui_client):
    runs = ui_client.sandbox["agentic"] / "runs" / "cycle-20260101-000000"
    checks = runs / "checks-1"
    checks.mkdir(parents=True)
    (checks / "pytest.log").write_text("$ pytest\nexit: 0\n\nall good",
                                       encoding="utf-8")
    r = ui_client.get("/api/v1/logs/cycle-20260101-000000/pytest")
    assert r.status_code == 200
    assert "all good" in r.json()["content"]


def test_log_content_is_redacted(ui_client):
    runs = ui_client.sandbox["agentic"] / "runs" / "cycle-20260102-000000"
    runs.mkdir(parents=True)
    (runs / "leaky.log").write_text(
        "exit: 1\ntoken sk-abcdefghijklmnop1234567890 leaked",
        encoding="utf-8")
    r = ui_client.get("/api/v1/logs/cycle-20260102-000000/leaky")
    assert r.status_code == 200
    assert "sk-abcdefghijklmnop" not in r.json()["content"]
    assert "[REDACTED]" in r.json()["content"]


def test_plan_path_outside_repo_rejected(ui_client, tmp_path):
    outside = tmp_path / "outside-plan.md"
    outside.write_text("# plan\n" + "content " * 20, encoding="utf-8")
    r = ui_client.post("/api/v1/project/plan/preview",
                       json={"plan_path": str(outside)})
    assert r.status_code == 422
    assert "repository root" in r.json()["detail"]


def test_plan_path_env_file_rejected(ui_client):
    repo = ui_client.sandbox["repo"]
    (repo / ".env.txt").write_text("SECRET=1", encoding="utf-8")
    r = ui_client.post("/api/v1/project/plan/preview",
                       json={"plan_path": ".env.txt"})
    assert r.status_code == 422


def test_plan_preview_requires_exactly_one_source(ui_client):
    assert ui_client.post("/api/v1/project/plan/preview",
                          json={}).status_code == 422
    assert ui_client.post(
        "/api/v1/project/plan/preview",
        json={"plan_text": "x" * 50, "plan_path": "a.md"}).status_code == 422


def test_no_credential_exposure_in_any_get(ui_client, monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "sk-secretsecretsecret123456")
    for path in ("/api/v1/project", "/api/v1/agents", "/api/v1/backends",
                 "/api/v1/capacity", "/api/v1/verification",
                 "/api/v1/settings", "/api/v1/doctor",
                 "/api/v1/project/activity"):
        r = ui_client.get(path)
        assert r.status_code == 200, path
        assert "sk-secretsecretsecret" not in r.text, path


def test_settings_get_never_returns_env_values(ui_client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-abcdef1234567890abcdef")
    r = ui_client.get("/api/v1/settings")
    assert "sk-abcdef" not in r.text


# -- state endpoints -----------------------------------------------------------

def test_health_and_doctor(ui_client):
    assert ui_client.get("/api/v1/health").json()["ok"] is True
    doctor = ui_client.get("/api/v1/doctor").json()
    assert "checks" in doctor and isinstance(doctor["checks"], list)


def test_project_snapshot_no_project(ui_client):
    snap = ui_client.get("/api/v1/project").json()
    assert snap["exists"] is False
    assert snap["scheduler"]["state"] == "idle"
    assert snap["branch"] == "agentic/project"


def test_project_snapshot_with_backlog(ui_client):
    seed_project(ui_client.sandbox)
    snap = ui_client.get("/api/v1/project").json()
    assert snap["exists"] is True
    assert snap["backlog_summary"] == {"pending": 1, "done": 1}
    assert snap["next_task"]["id"] == "T-1"
    backlog = ui_client.get("/api/v1/project/backlog").json()["tasks"]
    assert [t["id"] for t in backlog] == ["T-1", "T-2"]
    milestones = ui_client.get("/api/v1/project/milestones").json()
    assert milestones["milestones"][0]["id"] == "M1"
    plan = ui_client.get("/api/v1/project/plan").json()
    assert "test plan" in plan["plan"]


def test_agents_snapshot_shape(ui_client):
    agents = ui_client.get("/api/v1/agents").json()["agents"]
    ids = [a["id"] for a in agents]
    assert ids == ["architect", "conductor", "coder", "qa", "security",
                   "gate"]
    gate = agents[-1]
    assert gate["ai"] is False
    coder = agents[2]
    assert coder["can_edit"] is True
    for read_only in (agents[0], agents[1], agents[3], agents[4]):
        assert read_only["can_edit"] is False


def test_capacity_estimates_never_claim_quota(ui_client):
    snap = ui_client.get("/api/v1/capacity").json()
    assert "estimated from local history" in snap["note"]


def test_verification_reports_configured_checks(ui_client):
    snap = ui_client.get("/api/v1/verification").json()
    assert snap["no_checks_is_blocking"] is True
    assert isinstance(snap["commands"], list)


def test_activity_handles_malformed_lines(ui_client):
    memory = ui_client.sandbox["agentic"] / "memory"
    (memory / "decisions.jsonl").write_text(
        '{"event": "ok_line"}\nNOT JSON sk-abcdefghijklmnop12345678\n',
        encoding="utf-8")
    entries = ui_client.get("/api/v1/project/activity").json()["entries"]
    assert entries[0]["event"] == "ok_line"
    assert entries[1]["event"] == "malformed"
    assert "sk-abcdefghijklmnop" not in json.dumps(entries)


# -- controls ---------------------------------------------------------------------

def test_pause_resume_roundtrip(ui_client):
    assert ui_client.post("/api/v1/project/pause").json()["status"] == \
        "paused"
    snap = ui_client.get("/api/v1/project").json()
    assert snap["scheduler"]["state"] == "paused"
    assert ui_client.post("/api/v1/project/resume").json()["status"] == \
        "idle"


def test_project_start_conflicts_when_project_exists(ui_client):
    seed_project(ui_client.sandbox)
    r = ui_client.post("/api/v1/project/start",
                       json={"plan_text": "plan " * 20})
    assert r.status_code == 409


def test_project_start_rejects_short_plan(ui_client):
    r = ui_client.post("/api/v1/project/start", json={"plan_text": "hi"})
    assert r.status_code == 422


def test_smoke_test_requires_confirmation(ui_client):
    r = ui_client.post("/api/v1/backends/claude/smoke-test",
                       json={"confirm": False})
    assert r.status_code == 422
    assert "allowance" in r.json()["detail"]


def test_smoke_test_unknown_backend_404(ui_client):
    r = ui_client.post("/api/v1/backends/nope/smoke-test",
                       json={"confirm": True})
    assert r.status_code == 404


def test_breaker_reset_requires_confirmation(ui_client):
    from core.breaker import BreakerBoard
    memory = str(ui_client.sandbox["agentic"] / "memory")
    board = BreakerBoard(memory)
    board.record_failure("claude", "usage_limit", retry_after_seconds=3600)
    assert ui_client.post("/api/v1/backends/claude/reset-breaker",
                          json={"confirm": False}).status_code == 422
    r = ui_client.post("/api/v1/backends/claude/reset-breaker",
                       json={"confirm": True})
    assert r.status_code == 200
    assert BreakerBoard(memory).state("claude") == "available"


def test_backends_snapshot_auth_unknown_not_usable(sandbox):
    from ui.app import create_app
    sandbox["cfg"]["backends"] = {
        "claude": {"type": "cli", "kind": "configured", "binary": "claude"}}
    detector = lambda cfg: ({  # noqa: E731
        "claude": {"installed": True, "version": "1.0", "auth": "unknown"},
    }, {})
    app = create_app(load_cfg=lambda: sandbox["cfg"], detector=detector)
    client = TestClient(app, base_url="http://127.0.0.1")
    backends = {b["name"]: b
                for b in client.get("/api/v1/backends").json()["backends"]}
    assert backends["claude"]["usable"] is False   # unknown != authenticated
    assert backends["claude"]["auth"] == "unknown"


# -- operations (duplicate prevention, tracking) --------------------------------

def test_project_run_returns_operation_and_blocks_duplicates(ui_client,
                                                             monkeypatch):
    import core.project as project_mod
    started = []

    def slow_run(cfg, **kw):
        started.append(1)
        time.sleep(0.4)
        return {"status": "not_eligible", "reason": "test"}
    monkeypatch.setattr(project_mod, "project_run", slow_run)

    first = ui_client.post("/api/v1/project/run")
    assert first.status_code == 200
    op = first.json()
    assert op["status"] == "running" and op["kind"] == "project.run"

    duplicate = ui_client.post("/api/v1/project/run")
    assert duplicate.status_code == 409

    deadline = time.time() + 5
    while time.time() < deadline:
        current = ui_client.get("/api/v1/operations/%s" % op["id"]).json()
        if current["status"] != "running":
            break
        time.sleep(0.05)
    assert current["status"] == "succeeded"
    assert current["result"]["status"] == "not_eligible"
    assert started == [1]

    listing = ui_client.get("/api/v1/operations").json()["operations"]
    assert any(o["id"] == op["id"] for o in listing)


def test_operation_failure_is_reported_not_raised(ui_client, monkeypatch):
    import core.project as project_mod

    def boom(cfg, **kw):
        raise RuntimeError("backend exploded")
    monkeypatch.setattr(project_mod, "final_audit", boom)
    op = ui_client.post("/api/v1/project/review").json()
    deadline = time.time() + 5
    while time.time() < deadline:
        current = ui_client.get("/api/v1/operations/%s" % op["id"]).json()
        if current["status"] != "running":
            break
        time.sleep(0.05)
    assert current["status"] == "failed"
    assert "backend exploded" in current["error"]
    assert "trace" not in current   # tracebacks stay server-side


# -- settings ----------------------------------------------------------------------

def test_settings_roundtrip_and_machine_persistence(ui_client):
    r = ui_client.put("/api/v1/settings", json={
        "cooling": {"after_success_minutes": 45,
                    "after_failure_minutes": 20,
                    "minimum_minutes": 5, "maximum_minutes": 200},
        "ui": {"port": 9001, "theme": "light", "open_browser": False},
        "notifications": {"desktop": False},
    })
    assert r.status_code == 200, r.text
    machine = ui_client.sandbox["agentic"] / "config.machine.yaml"
    data = yaml.safe_load(machine.read_text(encoding="utf-8"))
    assert data["scheduler"]["cooling"]["after_success_minutes"] == 45
    assert data["ui"]["port"] == 9001
    assert data["notifications"]["desktop"] is False


def test_settings_validation_rejects_bad_values(ui_client):
    cases = [
        {"interaction": {"mode": "yolo"}},
        {"cycle": {"target_duration_minutes": 100,
                   "maximum_duration_minutes": 5}},
        {"capacity": {"safety_multiplier": 99}},
        {"routing": {"mode": "simple", "primary": "not-a-backend",
                     "fallbacks": []}},
        {"ui": {"port": 80}},
        {"operating_window": {"enabled": True, "start": "25:99",
                              "stop": "26:00"}},
        {"unknown_section": {"x": 1}},
        {"limits": {"claude": {"maximum_calls_per_hour": -5}}},
    ]
    for body in cases:
        r = ui_client.put("/api/v1/settings", json=body)
        assert r.status_code == 422, body


def test_settings_refuses_credential_shaped_keys(ui_client):
    r = ui_client.put("/api/v1/settings",
                      json={"ui": {"api_key": "sk-nope"}})
    assert r.status_code == 422


def test_settings_routing_valid_backends_accepted(ui_client):
    r = ui_client.put("/api/v1/settings", json={
        "routing": {"mode": "simple", "primary": "claude",
                    "fallbacks": ["ollama", "codex"]}})
    assert r.status_code == 200
    saved = ui_client.get("/api/v1/settings").json()
    assert saved["routing"]["primary"] == "claude"
    assert saved["routing"]["fallbacks"] == ["ollama", "codex"]


def test_machine_config_never_contains_secrets_after_updates(ui_client):
    ui_client.put("/api/v1/settings", json={
        "routing": {"mode": "simple", "primary": "ollama", "fallbacks": []}})
    text = (ui_client.sandbox["agentic"] /
            "config.machine.yaml").read_text(encoding="utf-8")
    assert "sk-" not in text and "token" not in text.lower()


# -- SSE / event bus -----------------------------------------------------------------

def test_event_bus_publish_subscribe_and_replay():
    from ui.bus import EventBus, sse_format
    bus = EventBus()
    q = bus.subscribe()
    e1 = bus.publish("state", {"changed": "scheduler"})
    got = q.get(timeout=1)
    assert got["type"] == "state" and got["data"]["changed"] == "scheduler"
    bus.publish("activity", {"entry": {"event": "x"}})
    replayed = bus.subscribe(last_event_id=str(e1["id"]))
    assert replayed.get(timeout=1)["type"] == "activity"
    text = sse_format(got)
    assert text.startswith("id: 1\nevent: state\n")
    bus.unsubscribe(q)


def test_sse_endpoint_streams_operation_events(ui_client, monkeypatch):
    import core.project as project_mod
    monkeypatch.setattr(project_mod, "project_run",
                        lambda cfg, **kw: {"status": "not_eligible",
                                           "reason": "t"})
    ui_client.post("/api/v1/project/run")
    # last_event_id=0 replays the buffered operation events; max_seconds
    # bounds the stream so the request completes like a reconnect cycle
    with ui_client.stream(
            "GET",
            "/api/v1/events?last_event_id=0&max_seconds=2") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(
            "text/event-stream")
        collected = "".join(response.iter_text())
    assert "event: operation" in collected
    assert "project.run" in collected


def test_watcher_emits_state_and_activity_events(ui_client):
    from ui.watch import StateWatcher
    from ui.bus import EventBus
    bus = EventBus()
    q = bus.subscribe()
    agentic = str(ui_client.sandbox["agentic"])
    watcher = StateWatcher(agentic, bus)
    memory = os.path.join(agentic, "memory")
    os.makedirs(memory, exist_ok=True)
    decisions = os.path.join(memory, "decisions.jsonl")
    with open(decisions, "w", encoding="utf-8") as fh:
        fh.write('{"event": "old"}\n')
    watcher._decisions_pos = os.path.getsize(decisions)
    with open(decisions, "a", encoding="utf-8") as fh:
        fh.write('{"event": "fresh", "backend": "claude"}\n')
    watcher._tick()
    event = q.get(timeout=1)
    assert event["type"] == "activity"
    assert event["data"]["entry"]["event"] == "fresh"


# -- serve helpers ----------------------------------------------------------------------

def test_pick_port_skips_busy_port():
    import socket
    from ui.serve import pick_port
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        busy = sock.getsockname()[1]
        picked = pick_port("127.0.0.1", busy)
        assert picked != busy and picked > busy


def test_run_ui_refuses_non_loopback(sandbox, capsys):
    from ui.serve import run_ui
    rc = run_ui(sandbox["cfg"], host="0.0.0.0")
    assert rc == 2
    assert "loopback" in capsys.readouterr().err
