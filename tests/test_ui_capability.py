"""Phase 12 -- Dashboard capability views: read-only snapshot endpoints
for the Capability Plan/Graph/setup-actions inbox/Completion Contract
(per project) and the Model Capability Registry/frontier capacity
(platform-wide). No live model/provider call is ever made from a
dashboard read."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

NO_DETECT = lambda cfg: ({}, {})  # noqa: E731

SEO_PLAN_MD = """---
project_type: static_site
---
## Product Vision

A marketing website for a boutique hotel.

## Functional Requirements

- The hotel website must rank for boutique hotel searches in Kathmandu.
"""


@pytest.fixture
def ui_client(sandbox, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_HOME", str(tmp_path / "home"))
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
    client.tmp = tmp_path
    return client


def add_app_folder(client, name="demo-app", plan_text="# plan\n"):
    root = client.tmp / "apps" / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "plan.md").write_text(plan_text, encoding="utf-8")
    r = client.post("/api/v1/portfolio/add",
                    json={"name": name, "root": str(root)})
    assert r.status_code == 200, r.text
    return r.json()


def test_capability_snapshot_empty_before_planning(ui_client):
    record = add_app_folder(ui_client)
    pid = record["id"]
    ui_client.post("/api/v1/portfolio/%s/init" % pid, json={})
    snap = ui_client.get("/api/v1/portfolio/%s/capability" % pid).json()
    assert snap["project_id"] == pid
    assert snap["plan_summary"] is None
    assert snap["graph_summary"] is None
    assert snap["setup_actions"] == []
    assert snap["completion_contract"] is None


def test_capability_snapshot_unknown_project_404(ui_client):
    assert ui_client.get(
        "/api/v1/portfolio/does-not-exist/capability").status_code == 404


def test_capability_snapshot_reflects_plan_and_graph(ui_client):
    record = add_app_folder(ui_client, name="hotel-app", plan_text=SEO_PLAN_MD)
    pid = record["id"]
    ui_client.post("/api/v1/portfolio/%s/init" % pid, json={})

    from core import projectops
    from core.registry import ProjectRegistry
    registry = ProjectRegistry()
    reg_record = registry.get(pid)
    plan = projectops.analyse_capabilities(registry, reg_record)
    projectops.save_capability_plan(registry, pid, plan)
    graph = projectops.build_capability_graph(registry, reg_record, plan=plan)
    projectops.save_capability_graph(registry, pid, graph)

    snap = ui_client.get("/api/v1/portfolio/%s/capability" % pid).json()
    assert "technical_seo" in snap["plan_summary"]["required_capabilities"]
    assert "deploy_to_production" in snap["plan_summary"]["protected_actions"]
    assert snap["graph_summary"]["capability_count"] > 0
    assert "unresolved" in snap["graph_summary"]["by_state"]


def test_orchestration_snapshot_empty_without_registry(ui_client):
    snap = ui_client.get("/api/v1/orchestration").json()
    assert snap["records"] == []
    assert snap["generated_at"] is None
    assert snap["frontier_capacity"] is None


def test_orchestration_snapshot_reflects_saved_registry(ui_client):
    from core.modelcap import ModelCapabilityRegistry, model_record, \
        save_registry
    memory_dir = str(ui_client.sandbox["agentic"] / "memory")
    registry = ModelCapabilityRegistry([
        model_record(backend="codex", provider="codex", model_id="gpt-5",
                    available=True, reasoning_class="frontier")])
    save_registry(memory_dir, registry)
    snap = ui_client.get("/api/v1/orchestration").json()
    assert len(snap["records"]) == 1
    assert snap["records"][0]["backend"] == "codex"
    assert snap["frontier_capacity"]["status"] == "ok"
