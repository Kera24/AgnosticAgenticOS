"""MP Phase 9 — dashboard multi-project API: portfolio, fleet, auth,
mcp, skills marketplace. Confirmation gates and no secret leakage."""
import json
import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

NO_DETECT = lambda cfg: ({}, {})  # noqa: E731


@pytest.fixture
def ui_client(sandbox, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_HOME", str(tmp_path / "home"))
    from core import setupwiz
    monkeypatch.setattr(setupwiz, "detect_backends",
                        lambda cfg, **kw: ({}, {}))
    # auth probes must never spawn real CLIs inside tests
    import core.authx as authx
    monkeypatch.setattr(authx, "backend_auth_report",
                        lambda cfg, mem, **kw: {
                            "mock": authx._report("mock", "authenticated",
                                                  autonomous_ready=True)})
    from ui.app import create_app
    cfg = sandbox["cfg"]
    cfg["backends"] = {"mock": {"type": "api", "provider": "mock",
                                "model": "mock-model"}}
    cfg["routing"] = {"mode": "simple", "primary": "mock", "fallbacks": []}
    app = create_app(load_cfg=lambda: cfg, detector=NO_DETECT,
                     enable_project_selection=True)
    client = TestClient(app, base_url="http://127.0.0.1")
    client.sandbox = sandbox
    client.tmp = tmp_path
    return client


def add_app_folder(client, name="demo-app"):
    root = client.tmp / "apps" / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "plan.md").write_text("# plan\n", encoding="utf-8")
    r = client.post("/api/v1/portfolio/add",
                    json={"name": name, "root": str(root)})
    assert r.status_code == 200, r.text
    return r.json()


# -- portfolio ---------------------------------------------------------------------

def test_portfolio_add_and_snapshot(ui_client):
    record = add_app_folder(ui_client)
    snap = ui_client.get("/api/v1/portfolio").json()
    assert len(snap["projects"]) == 1
    project = snap["projects"][0]
    assert project["id"] == record["id"]
    assert os.path.isabs(project["root_path"])
    assert project["state"] == "uninitialised"
    assert "waiting_reason" in project
    assert snap["runtime_home"]
    # no credentials anywhere in the payload
    assert "api_key" not in json.dumps(snap).lower()



def test_portfolio_exposes_final_audit_failure_evidence(ui_client):
    record = add_app_folder(ui_client)
    pid = record["id"]
    ui_client.post("/api/v1/portfolio/%s/init" % pid, json={})
    project = ui_client.get("/api/v1/portfolio").json()["projects"][0]
    audit_path = os.path.join(
        project["runtime_dir"], "project", "final-audit.yaml")
    with open(audit_path, "w", encoding="utf-8") as fh:
        json.dump({
            "complete": False,
            "checks": {"local_browser_smoke": False},
            "failure_details": [{
                "check": "local_browser_smoke",
                "name": "local-static-app-smoke",
                "detail": "index.html has no application root",
            }],
        }, fh)

    project = ui_client.get("/api/v1/portfolio").json()["projects"][0]

    assert project["final_audit"]["complete"] is False
    assert project["final_audit"]["failed_checks"] == [
        "local_browser_smoke"]
    assert project["final_audit"]["failure_details"][0]["detail"] == \
        "index.html has no application root"


def test_portfolio_add_rejects_bad_paths(ui_client):
    r = ui_client.post("/api/v1/portfolio/add",
                       json={"name": "ghost",
                             "root": str(ui_client.tmp / "missing")})
    assert r.status_code == 422
    assert "does not exist" in r.json()["detail"]


def test_portfolio_lifecycle_actions_and_confirmations(ui_client):
    record = add_app_folder(ui_client)
    pid = record["id"]
    r = ui_client.post("/api/v1/portfolio/%s/init" % pid, json={})
    assert r.status_code == 200 and r.json()["ok"]
    snap = ui_client.get("/api/v1/portfolio").json()
    assert snap["projects"][0]["state"] == "ready"
    # destructive actions demand confirmation
    assert ui_client.post("/api/v1/portfolio/%s/archive" % pid,
                          json={}).status_code == 422
    r = ui_client.post("/api/v1/portfolio/%s/archive" % pid,
                       json={"confirm": True})
    assert r.status_code == 200
    # remove never deletes the application folder
    ui_client.post("/api/v1/portfolio/%s/remove" % pid,
                   json={"confirm": True})
    assert os.path.exists(os.path.join(record["root_path"], "plan.md"))
    assert ui_client.post("/api/v1/portfolio/%s/init" % pid,
                          json={}).status_code == 404
    assert ui_client.post("/api/v1/portfolio/x/frobnicate",
                          json={}).status_code == 404



def test_portfolio_start_uses_registered_project_overlay(ui_client,
                                                         monkeypatch):
    record = add_app_folder(ui_client)
    pid = record["id"]
    ui_client.post("/api/v1/portfolio/%s/init" % pid, json={})
    captured = {}

    import core.project as project_mod

    def fake_start(cfg, plan, **kw):
        from core import config as config_mod
        captured["project_dir"] = cfg["runtime"]["project_dir"]
        captured["root"] = str(config_mod.repo_root(cfg))
        captured["plan"] = plan
        return {"status": "started"}

    monkeypatch.setattr(project_mod, "project_start", fake_start)
    response = ui_client.post("/api/v1/portfolio/%s/start" % pid, json={})
    assert response.status_code == 200, response.text
    operation = response.json()
    assert operation["kind"] == "portfolio.project.start"

    import time
    deadline = time.time() + 5
    while time.time() < deadline:
        current = ui_client.get(
            "/api/v1/operations/%s" % operation["id"]).json()
        if current["status"] != "running":
            break
        time.sleep(0.05)
    assert current["status"] == "succeeded"
    assert captured["root"] == record["root_path"]
    assert captured["plan"] == str(
        ui_client.tmp / "apps" / "demo-app" / "plan.md")
    assert pid in captured["project_dir"]



def test_portfolio_doctor_and_pause(ui_client):
    pid = add_app_folder(ui_client)["id"]
    ui_client.post("/api/v1/portfolio/%s/init" % pid, json={})
    doctor = ui_client.post("/api/v1/portfolio/%s/doctor" % pid,
                            json={}).json()
    assert doctor["ok"] and doctor["checks"]
    ui_client.post("/api/v1/portfolio/%s/pause" % pid, json={})
    snap = ui_client.get("/api/v1/portfolio").json()
    assert snap["projects"][0]["state"] == "paused"
    ui_client.post("/api/v1/portfolio/%s/resume" % pid, json={})
    assert ui_client.get("/api/v1/portfolio").json()["projects"][0][
        "state"] in ("ready", "queued")


# -- fleet -------------------------------------------------------------------------

def test_fleet_snapshot_and_pause(ui_client):
    add_app_folder(ui_client)
    snap = ui_client.get("/api/v1/fleet").json()
    assert snap["global_pause"] is False
    assert snap["limits"]["maximum_active_projects"] == 4
    assert "slots" in snap and "would_start" in snap
    assert ui_client.post("/api/v1/fleet/pause",
                          json={}).status_code == 422
    ui_client.post("/api/v1/fleet/pause", json={"confirm": True})
    assert ui_client.get("/api/v1/fleet").json()["global_pause"] is True
    ui_client.post("/api/v1/fleet/resume")
    assert ui_client.get("/api/v1/fleet").json()["global_pause"] is False


def test_fleet_preview_is_read_only(ui_client):
    pid = add_app_folder(ui_client)["id"]
    ui_client.post("/api/v1/portfolio/%s/init" % pid, json={})
    ui_client.post("/api/v1/portfolio/%s/enable" % pid, json={})
    before = ui_client.get("/api/v1/fleet").json()
    after = ui_client.get("/api/v1/fleet").json()
    # repeated previews claim no slots
    assert after["slots"]["active_projects"] == 0
    assert before["states"] == after["states"]


# -- auth ---------------------------------------------------------------------------

def test_auth_endpoint(ui_client):
    body = ui_client.get("/api/v1/auth").json()
    assert body["backends"]["mock"]["state"] == "authenticated"


# -- mcp ----------------------------------------------------------------------------

def test_mcp_endpoints_with_confirmation(ui_client):
    from core.mcp import MCPGateway
    from core.registry import ProjectRegistry
    gateway = MCPGateway(ui_client.sandbox["cfg"], ProjectRegistry().home)
    record = gateway.add("local-tools", transport="stdio", command="npx x")
    servers = ui_client.get("/api/v1/mcp").json()["servers"]
    assert servers[0]["id"] == record["id"]
    assert ui_client.post("/api/v1/mcp/%s/enable" % record["id"],
                          json={"confirm": True}).status_code == 422
    ui_client.post("/api/v1/mcp/%s/review" % record["id"], json={})
    r = ui_client.post("/api/v1/mcp/%s/enable" % record["id"],
                       json={"confirm": True})
    assert r.status_code == 200 and r.json()["enabled"]


# -- skills marketplace --------------------------------------------------------------

def test_market_endpoints(ui_client, tmp_path):
    mirror = tmp_path / "mirror" / "pdf-tools"
    mirror.mkdir(parents=True)
    (mirror / "skill.yaml").write_text(
        "id: pdf-tools\ndescription: pdf work\nlicense: MIT\n"
        "triggers: [pdf]\n", encoding="utf-8")
    (mirror / "SKILL.md").write_text("# pdf\n\nguide\n", encoding="utf-8")
    ui_client.sandbox["cfg"]["skills"] = {
        "enabled": True,
        "registries": [{"name": "m", "type": "directory",
                        "path": str(tmp_path / "mirror")}]}
    from core.skillmarket import SkillMarket
    from core import config as config_mod
    from core.registry import ProjectRegistry
    SkillMarket(ui_client.sandbox["cfg"], str(config_mod.AGENTIC_DIR),
                ProjectRegistry().home).discover("pdf")
    body = ui_client.get("/api/v1/skills/market").json()
    assert body["candidates"][0]["id"] == "pdf-tools"
    # approve requires quarantine AND confirmation
    assert ui_client.post("/api/v1/skills/market/pdf-tools/approve",
                          json={}).status_code == 422
    ui_client.post("/api/v1/skills/market/pdf-tools/quarantine", json={})
    r = ui_client.post("/api/v1/skills/market/pdf-tools/approve",
                       json={"confirm": True})
    assert r.status_code == 200
    assert r.json()["reviewed"] is True


# -- security still applies ----------------------------------------------------------

def test_portfolio_mutations_loopback_guarded(ui_client):
    r = ui_client.post("/api/v1/fleet/pause", json={"confirm": True},
                       headers={"Host": "evil.example.com"})
    assert r.status_code == 403
    r2 = ui_client.post("/api/v1/portfolio/add",
                        json={"name": "x", "root": "C:\\x"},
                        headers={"Origin": "https://evil.example.com"})
    assert r2.status_code == 403


def test_select_project_drives_project_api_overlay(ui_client, monkeypatch):
    first = add_app_folder(ui_client, "first-app")
    second = add_app_folder(ui_client, "second-app")

    selected = ui_client.post(
        "/api/v1/portfolio/%s/select" % second["id"], json={})
    assert selected.status_code == 200
    assert selected.json()["project_id"] == second["id"]
    assert ui_client.get("/api/v1/project-selection").json() == {
        "project_id": second["id"], "name": second["name"]}

    captured = {}
    from ui import snapshots

    def fake_snapshot(configuration):
        captured["root"] = str(configuration["project"]["repository_root"])
        captured["project_dir"] = configuration["runtime"]["project_dir"]
        return {"exists": False}

    monkeypatch.setattr(snapshots, "project_snapshot", fake_snapshot)
    response = ui_client.get("/api/v1/project")
    assert response.status_code == 200
    assert captured["root"] == second["root_path"]
    assert second["id"] in captured["project_dir"]
    assert captured["root"] != first["root_path"]


def test_select_project_rejects_unknown_registry_id(ui_client):
    response = ui_client.post(
        "/api/v1/portfolio/not-registered/select", json={})
    assert response.status_code == 404
