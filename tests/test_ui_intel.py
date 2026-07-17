"""Phase 10 — dashboard API for context/memory/knowledge/skills/routing:
contracts, confirmation gates, path confinement, no provider calls."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

NO_DETECT = lambda cfg: ({}, {})  # noqa: E731


@pytest.fixture
def ui_client(sandbox, monkeypatch):
    from core import setupwiz
    monkeypatch.setattr(setupwiz, "detect_backends",
                        lambda cfg, **kw: ({}, {}))
    from ui.app import create_app
    cfg = sandbox["cfg"]
    cfg["backends"] = {"mock": {"type": "api", "provider": "mock",
                               "model": "mock-model"}}
    cfg["routing"] = {"mode": "simple", "primary": "mock", "fallbacks": []}
    app = create_app(load_cfg=lambda: cfg, detector=NO_DETECT)
    client = TestClient(app, base_url="http://127.0.0.1")
    client.sandbox = sandbox
    return client


def seed_memory(sandbox, **kw):
    from core.memsvc import MemoryService
    service = MemoryService(str(sandbox["agentic"] / "memory"), "test")
    return service.save(kw.pop("type", "implementation_decision"),
                        kw.pop("title", "use sqlite"),
                        kw.pop("summary", "sqlite is fine"), **kw)


# -- context ---------------------------------------------------------------------

def test_context_snapshot_contract(ui_client, sandbox):
    from core.context.compose import compose
    compose(sandbox["cfg"], "qa", "review things",
            {"work_order": {"item": "x"}},
            memory_dir=str(sandbox["agentic"] / "memory"))
    r = ui_client.get("/api/v1/context")
    assert r.status_code == 200
    body = r.json()
    assert body["code_intelligence"]["provider"] in ("native", "none")
    assert body["totals"]["measurement"] == "estimated"
    assert len(body["packages"]) == 1
    package = body["packages"][0]
    assert package["role"] == "qa"
    assert "included" in package and "omitted" in package


def test_context_search_requires_query(ui_client):
    assert ui_client.get("/api/v1/context/search").status_code == 422
    r = ui_client.get("/api/v1/context/search?q=app")
    assert r.status_code == 200
    assert "results" in r.json()


# -- memory -----------------------------------------------------------------------

def test_memory_progressive_disclosure(ui_client, sandbox):
    rid = seed_memory(sandbox, details="full explanation")
    listing = ui_client.get("/api/v1/memory?q=sqlite").json()
    assert listing["records"][0]["id"] == rid
    assert "details" not in listing["records"][0]     # compact layer
    timeline = ui_client.get("/api/v1/memory/%s/timeline" % rid).json()
    assert [t["id"] for t in timeline["timeline"]] == [rid]
    details = ui_client.get("/api/v1/memory/records?ids=%s" % rid).json()
    assert details["records"][0]["details"] == "full explanation"


def test_memory_forget_requires_confirmation(ui_client, sandbox):
    rid = seed_memory(sandbox)
    r = ui_client.post("/api/v1/memory/forget", json={"id": rid})
    assert r.status_code == 422                       # no confirm
    r = ui_client.post("/api/v1/memory/forget",
                       json={"id": rid, "confirm": True})
    assert r.status_code == 200 and r.json()["forgotten"] == 1
    r = ui_client.post("/api/v1/memory/forget",
                       json={"id": rid, "confirm": True})
    assert r.status_code == 404                       # already gone


# -- knowledge ----------------------------------------------------------------------

def test_knowledge_snapshot_and_document(ui_client, sandbox):
    from core.knowledge import KnowledgeVault
    vault = KnowledgeVault(sandbox["cfg"], str(sandbox["agentic"]))
    vault.write_doc("architecture/db.md", "db", "architecture", "DB",
                    "SQLite everywhere.")
    snapshot = ui_client.get("/api/v1/knowledge").json()
    assert snapshot["documents"] == 1
    assert snapshot["docs"][0]["path"] == "architecture/db.md"
    doc = ui_client.get(
        "/api/v1/knowledge/doc?path=architecture/db.md").json()
    assert "SQLite everywhere." in doc["generated"]
    assert doc["generated_intact"] is True


def test_knowledge_document_path_confined(ui_client):
    r = ui_client.get("/api/v1/knowledge/doc?path=../../.env")
    assert r.status_code in (404, 422, 500)
    r2 = ui_client.get("/api/v1/knowledge/doc?path=x.txt")
    assert r2.status_code == 422                      # markdown only


# -- skills ------------------------------------------------------------------------

def test_skills_listing_and_confirmed_toggle(ui_client, sandbox):
    import shutil
    import os
    src = os.path.join(os.path.dirname(__file__), "..", ".agentic",
                       "skills", "builtin")
    shutil.copytree(src, sandbox["agentic"] / "skills" / "builtin")
    listing = ui_client.get("/api/v1/skills").json()
    ids = {s["id"] for s in listing["skills"]}
    assert "testing" in ids
    # confirmation gate
    r = ui_client.post("/api/v1/skills/testing/disable",
                       json={"confirm": False})
    assert r.status_code == 422
    r = ui_client.post("/api/v1/skills/testing/disable",
                       json={"confirm": True})
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = ui_client.post("/api/v1/skills/testing/enable",
                       json={"confirm": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    # unknown action 404s
    assert ui_client.post("/api/v1/skills/testing/delete",
                          json={"confirm": True}).status_code == 404


# -- routing -----------------------------------------------------------------------

def test_routing_snapshot(ui_client, sandbox):
    from core.routing import capability_chain
    sandbox["cfg"]["routing"] = {
        "mode": "capability",
        "policies": {"reviewer_different_from_worker": True},
        "agents": {"coder": {"capabilities": {"coding": "high"}}}}
    capability_chain(sandbox["cfg"], "coder",
                     memory_dir=str(sandbox["agentic"] / "memory"))
    body = ui_client.get("/api/v1/routing").json()
    assert body["mode"] == "capability"
    assert body["policies"]["reviewer_different_from_worker"] is True
    assert body["decisions"] and body["decisions"][0]["role"] == "coder"


# -- security still applies to new endpoints ------------------------------------------

def test_new_mutations_are_loopback_guarded(ui_client):
    r = ui_client.post("/api/v1/memory/forget",
                       json={"id": "x", "confirm": True},
                       headers={"Host": "evil.example.com"})
    assert r.status_code == 403
    r = ui_client.post("/api/v1/skills/testing/disable",
                       json={"confirm": True},
                       headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403
