"""Phase 4 — knowledge vault: markdown validity, stable rewrites, user
sections, conflicts, links, Windows paths, .obsidian handling, recovery."""
import os

from conftest import Clock, seed_project, simple_task
from core.knowledge import (KnowledgeVault, knowledge_items,
                            update_knowledge, vault_config)


def vault_for(sandbox, clock=None):
    return KnowledgeVault(sandbox["cfg"], str(sandbox["agentic"]),
                          clock=clock)


def test_write_and_read_roundtrip(sandbox):
    vault = vault_for(sandbox)
    assert vault.write_doc("architecture/net.md", "net", "architecture",
                           "Network Design", "Services talk over HTTP.\n"
                           "See [[architecture/architecture]].") == "written"
    doc = vault.read_doc("architecture/net.md")
    assert doc["meta"]["id"] == "net"
    assert doc["meta"]["project"] == "test"
    assert doc["generated"].startswith("# Network Design")
    assert doc["generated_intact"]
    assert "user-notes:start" in doc["user_section"]


def test_unchanged_content_not_rewritten(sandbox):
    clock = Clock()
    vault = vault_for(sandbox, clock=clock)
    vault.write_doc("a.md", "a", "note", "T", "body")
    first = vault.read_doc("a.md")["meta"]["updated"]
    clock.advance(minutes=90)
    assert vault.write_doc("a.md", "a", "note", "T", "body") == "unchanged"
    assert vault.read_doc("a.md")["meta"]["updated"] == first
    clock.advance(minutes=5)
    assert vault.write_doc("a.md", "a", "note", "T", "new body") == "written"
    doc = vault.read_doc("a.md")
    assert doc["meta"]["updated"] != first
    assert doc["meta"]["created"] != doc["meta"]["updated"]  # created kept


def test_user_section_preserved_across_regeneration(sandbox):
    vault = vault_for(sandbox)
    vault.write_doc("b.md", "b", "note", "T", "v1")
    path = vault.path("b.md")
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    raw = raw.replace("_Your notes here survive regeneration._",
                      "MY IMPORTANT HUMAN NOTES")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(raw)
    assert vault.write_doc("b.md", "b", "note", "T", "v2") == "written"
    doc = vault.read_doc("b.md")
    assert "MY IMPORTANT HUMAN NOTES" in doc["user_section"]
    assert "v2" in doc["generated"]


def test_user_edit_in_generated_area_is_conflict(sandbox):
    vault = vault_for(sandbox)
    vault.write_doc("c.md", "c", "note", "T", "generated text")
    path = vault.path("c.md")
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(raw.replace("generated text", "human-edited text"))
    assert vault.write_doc("c.md", "c", "note", "T", "regenerated") \
        == "conflict"
    # user's file untouched, fresh content parked as .incoming.md
    assert "human-edited text" in open(path, encoding="utf-8").read()
    incoming = vault.read_doc("c.incoming.md")
    assert incoming and "regenerated" in incoming["generated"]
    assert any("conflict" in i for i in vault.validate())


def test_rebuild_from_project_state(sandbox):
    seed_project(sandbox, [simple_task()])
    results = vault_for(sandbox).rebuild()
    assert results["project-overview.md"] == "written"
    assert results["current-state.md"] == "written"
    assert results["milestones/m1.md"] == "written"
    vault = vault_for(sandbox)
    issues = vault.validate()
    assert issues == []
    # idempotent: second rebuild changes nothing
    again = vault_for(sandbox).rebuild()
    assert all(state in ("unchanged", "written") for state in again.values())
    assert again["project-overview.md"] == "unchanged"


def test_validate_detects_broken_links(sandbox):
    vault = vault_for(sandbox)
    vault.write_doc("d.md", "d", "note", "T", "see [[does-not-exist]]")
    assert any("broken link" in i for i in vault.validate())


def test_obsidian_workspace_never_indexed(sandbox):
    vault = vault_for(sandbox)
    vault.write_doc("e.md", "e", "note", "T", "body")
    obsdir = os.path.join(vault.root, ".obsidian")
    os.makedirs(obsdir, exist_ok=True)
    with open(os.path.join(obsdir, "workspace.json"), "w") as fh:
        fh.write("{}")
    with open(os.path.join(obsdir, "note.md"), "w") as fh:
        fh.write("should never be scanned")
    assert all(not d.startswith(".obsidian") for d in vault.documents())
    assert knowledge_items(sandbox["cfg"], str(sandbox["agentic"]),
                           "never scanned") == []


def test_partial_write_recovery(sandbox):
    vault = vault_for(sandbox)
    vault.write_doc("f.md", "f", "note", "T", "body")
    with open(vault.path("f.md") + ".tmp", "w") as fh:
        fh.write("interrupted half-write")
    assert vault.read_doc("f.md")["generated_intact"]   # tmp ignored
    assert "f.md.tmp" not in vault.documents()
    assert vault.write_doc("f.md", "f", "note", "T", "body2") == "written"


def test_path_escape_rejected(sandbox):
    vault = vault_for(sandbox)
    try:
        vault.path("../../outside.md")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    # Windows-style separators are normalized, not escapes
    vault.write_doc("sub\\dir\\win.md", "w", "note", "T", "body")
    assert vault.read_doc("sub/dir/win.md")


def test_knowledge_items_sections_only(sandbox):
    vault = vault_for(sandbox)
    vault.write_doc("architecture/db.md", "db", "architecture",
                    "Database Choice",
                    "## Storage\nWe use SQLite everywhere.\n\n"
                    "## Unrelated\nDeployment cadence is weekly.")
    items = knowledge_items(sandbox["cfg"], str(sandbox["agentic"]),
                            "sqlite storage")
    assert items
    assert all(i.trust_level == "untrusted" for i in items)
    assert all(i.category == "knowledge" for i in items)
    assert any("SQLite" in i.content for i in items)
    # a section, not the whole document
    assert all("Unrelated" not in i.content for i in items)


def test_update_knowledge_hook_disabled(sandbox):
    cfg = dict(sandbox["cfg"])
    cfg["knowledge"] = {"enabled": False}
    assert vault_config(cfg)["enabled"] is False
    assert update_knowledge(cfg, str(sandbox["agentic"])) is None
    assert not os.path.exists(os.path.join(str(sandbox["agentic"]),
                                           "knowledge"))


def test_redaction_in_vault(sandbox, monkeypatch):
    monkeypatch.setenv("VAULT_API_KEY", "topsecretvaultvalue")
    vault = vault_for(sandbox)
    vault.write_doc("g.md", "g", "note", "T",
                    "key is topsecretvaultvalue")
    raw = open(vault.path("g.md"), encoding="utf-8").read()
    assert "topsecretvaultvalue" not in raw
    assert "[REDACTED]" in raw
