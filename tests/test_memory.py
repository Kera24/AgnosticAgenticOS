"""Phase 3 — persistent memory: progressive disclosure, isolation,
redaction, supersession, expiry, recovery, bounded injection."""
import datetime
import json
import os

from conftest import Clock
from core.memsvc import (MemoryService, get_memory, memory_config,
                         memory_items)


def svc(tmp_path, project="proj-a", clock=None):
    return MemoryService(str(tmp_path / "memory"), project, clock=clock)


def test_save_search_details_roundtrip(tmp_path):
    service = svc(tmp_path)
    rid = service.save("architecture_decision", "Use SQLite for memory",
                       "SQLite chosen for zero-dependency persistence",
                       details="Full rationale: stdlib, WAL, portable.",
                       tags=["storage"], reviewer_verified=True)
    rows = service.search("sqlite persistence")
    assert [r["id"] for r in rows] == [rid]
    assert "details" not in rows[0]            # layer 1 stays compact
    full = service.details([rid])
    assert full[0]["details"].startswith("Full rationale")
    assert full[0]["tags"] == ["storage"]


def test_type_validation(tmp_path):
    try:
        svc(tmp_path).save("gossip", "t", "s")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_project_isolation(tmp_path):
    a = svc(tmp_path, "proj-a")
    b = svc(tmp_path, "proj-b")
    a.save("constraint", "python 3.11 only", "runtime constraint")
    assert a.search("python") and not b.search("python")
    assert b.status()["total_records"] == 0


def test_redaction_at_write(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_API_KEY", "supersecretvalue123")
    service = svc(tmp_path)
    rid = service.save("bug", "auth failure",
                       "request failed using supersecretvalue123",
                       details="header was sk-" + "a" * 24)
    record = service.details([rid])[0]
    assert "supersecretvalue123" not in json.dumps(record)
    assert "sk-" + "a" * 24 not in record["details"]
    assert "[REDACTED]" in record["compact_summary"]


def test_deduplication_updates_instead_of_duplicating(tmp_path):
    service = svc(tmp_path)
    r1 = service.save("preference", "tabs vs spaces", "spaces, 4-wide")
    r2 = service.save("preference", "tabs vs spaces", "spaces, 4-wide",
                      reviewer_verified=True)
    assert r1 == r2
    assert service.status()["total_records"] == 1
    assert service.search("spaces")[0]["reviewer_verified"] == 1


def test_supersession_excluded_everywhere(tmp_path):
    service = svc(tmp_path)
    old = service.save("implementation_decision", "http client",
                       "use requests")
    new = service.save("implementation_decision", "http client v2",
                       "use httpx instead", supersedes=old)
    active = service.search("http client", limit=10)
    assert [r["id"] for r in active] == [new]
    withsup = service.search("http client", include_superseded=True,
                             limit=10)
    assert {r["id"] for r in withsup} == {old, new}


def test_expiry(tmp_path):
    clock = Clock()
    service = svc(tmp_path, clock=clock)
    service.save("cycle_outcome", "cycle 1", "ok",
                 expires_at=(clock.now + datetime.timedelta(days=1))
                 .isoformat(timespec="seconds"))
    assert service.search("cycle")
    clock.advance(minutes=60 * 24 * 2)
    assert not service.search("cycle")          # expired, not returned
    assert service.details(
        [service.search("cycle", include_superseded=True)[0]["id"]]) \
        if service.search("cycle", include_superseded=True) else True


def test_timeline_window(tmp_path):
    service = svc(tmp_path)
    ids = [service.save("cycle_outcome", "cycle %d" % i, "outcome %d" % i)
           for i in range(10)]
    timeline = service.timeline(ids[5], window=2)
    titles = [t["title"] for t in timeline]
    assert titles == ["cycle 3", "cycle 4", "cycle 5", "cycle 6", "cycle 7"]


def test_forget_and_compact(tmp_path):
    clock = Clock()
    service = svc(tmp_path, clock=clock)
    keep = service.save("requirement", "must run offline", "no cloud calls")
    gone = service.save("bug", "flaky test", "test_x flakes")
    assert service.forget(gone) == 1
    old = service.save("implementation_decision", "old approach", "v1")
    service.save("implementation_decision", "new approach", "v2",
                 supersedes=old)
    clock.advance(minutes=60 * 24 * 365)
    removed = service.compact(retention_days=180)
    assert removed >= 1
    assert {r["id"] for r in service.search(limit=50)} >= {keep}


def test_corrupt_database_recovery(tmp_path):
    memdir = tmp_path / "memory"
    memdir.mkdir()
    (memdir / "memory.db").write_bytes(b"THIS IS NOT A SQLITE FILE" * 10)
    service = MemoryService(str(memdir), "proj-a")
    rid = service.save("resolution", "recovered", "db recreated")
    assert service.search("recovered")[0]["id"] == rid
    assert any(name.startswith("memory.db.corrupt-")
               for name in os.listdir(str(memdir)))


def test_sensitive_records_never_injected(tmp_path):
    service = svc(tmp_path)
    service.save("security_finding", "hardcoded token found",
                 "token in config.py", sensitive=True)
    assert not service.search("token")                       # default hides
    assert service.search("token", include_sensitive=True)   # explicit only


def test_memory_items_bounded_and_untrusted(tmp_path):
    cfg = {"project": {"name": "proj-a"},
           "memory": {"enabled": True, "inject_limit": 3}}
    memdir = str(tmp_path / "memory")
    service = MemoryService(memdir, "proj-a")
    for i in range(10):
        service.save("failed_attempt", "attempt %d at parser fix" % i,
                     "parser approach %d failed" % i)
    old = service.save("implementation_decision", "parser library",
                       "use pyparsing")
    service.save("implementation_decision", "parser library v2",
                 "use lark for the parser instead", supersedes=old)
    items = memory_items(cfg, memdir, "parser fix approach")
    assert 0 < len(items) <= 3                     # inject_limit, not history
    assert all(i.trust_level == "untrusted" for i in items)
    assert all(i.category == "memory" for i in items)
    texts = " ".join(i.content for i in items)
    assert "use pyparsing" not in texts            # superseded never injected
    # disabled memory injects nothing
    cfg["memory"]["enabled"] = False
    assert memory_items(cfg, memdir, "parser") == []


def test_memory_config_and_get_memory_defaults():
    cfg = {"project": {"name": "demo"}}
    assert memory_config(cfg)["enabled"] is True
    assert memory_config({"memory": {"enabled": False}})["enabled"] is False
    service = get_memory(cfg, ".")
    assert service.project_id == "demo"


def test_cycle_hooks_write_memory(sandbox):
    """A blocked task and its cycle outcome land in memory via the
    project-cycle hooks (deterministic writes, no model involved)."""
    from conftest import (FakeCaller, project_cfg, proj_order, seed_project,
                          simple_task)
    from core import project as project_mod
    from core.memsvc import MemoryService
    cfg = project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    order = proj_order(task, action="queue", queue_reason="needs human")
    caller = FakeCaller({"conductor": order})
    result = project_mod.run_cycle(cfg, caller=caller)
    assert result["status"] == "failure"
    service = MemoryService(str(sandbox["agentic"] / "memory"), "test")
    types = {r["type"] for r in service.search(limit=50)}
    assert "failed_attempt" in types
    assert "cycle_outcome" in types
