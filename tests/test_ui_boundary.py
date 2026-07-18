"""MP Phase 11 — desktop-ready boundary endpoints: readiness, version,
protected shutdown."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def ui_client(sandbox, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_HOME", str(tmp_path / "home"))
    from core import setupwiz
    monkeypatch.setattr(setupwiz, "detect_backends",
                        lambda cfg, **kw: ({}, {}))
    from ui.app import create_app
    app = create_app(load_cfg=lambda: sandbox["cfg"],
                     detector=lambda cfg: ({}, {}))
    return TestClient(app, base_url="http://127.0.0.1")


def test_readiness_reports_ready(ui_client):
    body = ui_client.get("/api/v1/readiness").json()
    assert body["ready"] is True
    assert body["problems"] == []


def test_version_reports_platform_revision(ui_client):
    body = ui_client.get("/api/v1/version").json()
    assert body["api"] == "v1"
    assert body["ui_version"]
    # sandboxed AGENTIC_DIR has no git repo -> honest None; a real install
    # returns the short revision string
    assert body["platform_revision"] is None or \
        len(body["platform_revision"]) >= 7


def test_shutdown_requires_confirmation_and_guards(ui_client,
                                                   monkeypatch):
    import ui.app as app_mod
    # never actually exit the test process
    exits = []
    import threading

    class FakeTimer:
        def __init__(self, delay, fn):
            exits.append(delay)

        def start(self):
            pass

    monkeypatch.setattr(threading, "Timer", FakeTimer)
    assert ui_client.post("/api/v1/shutdown", json={}).status_code == 422
    r = ui_client.post("/api/v1/shutdown", json={"confirm": True},
                       headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403               # origin guard applies
    r2 = ui_client.post("/api/v1/shutdown", json={"confirm": True})
    assert r2.status_code == 200
    assert r2.json()["shutting_down"] is True
    assert exits                              # exit was scheduled, mocked
