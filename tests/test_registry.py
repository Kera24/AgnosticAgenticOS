"""MP Phases 1–2 — project registry and state boundaries: validation,
atomicity, isolation, redirection, legacy migration."""
import json
import os
import subprocess

import pytest

from core.registry import (ProjectRegistry, RegistryError, canonical,
                           runtime_home)


@pytest.fixture
def reg(tmp_path):
    return ProjectRegistry(home=str(tmp_path / "home"))


def make_app(tmp_path, name="app-one", git=True, files=True):
    root = tmp_path / "apps" / name
    root.mkdir(parents=True)
    if files:
        (root / "plan.md").write_text("# Plan\n\nBuild %s.\n" % name,
                                      encoding="utf-8")
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    if git:
        for args in (["init", "-b", "main"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@t",
                      "commit", "-m", "init"]):
            subprocess.run(["git"] + args, cwd=str(root),
                           capture_output=True)
    return str(root)


# -- registration rules ---------------------------------------------------------

def test_add_resolves_absolute_and_assigns_stable_id(reg, tmp_path):
    root = make_app(tmp_path)
    record = reg.add("Restaurant Ordering", root, plan="plan.md")
    assert record["id"] == "restaurant-ordering"
    assert os.path.isabs(record["root_path"])
    assert record["canonical_root_path"] == canonical(root)
    assert record["docker_compose_project"] == "agentic-restaurant-ordering"
    assert record["status"] == "registered" and record["enabled"] is False
    # id is stable across loads
    assert reg.get("restaurant-ordering")["created_at"]


def test_nonexistent_root_rejected_unless_creating(reg, tmp_path):
    with pytest.raises(RegistryError, match="does not exist"):
        reg.add("ghost", str(tmp_path / "nope"))
    record = reg.add("newborn", str(tmp_path / "fresh"), create=True)
    assert os.path.isdir(record["root_path"])


def test_duplicate_canonical_path_rejected_case_insensitive(reg, tmp_path):
    root = make_app(tmp_path)
    reg.add("one", root)
    with pytest.raises(RegistryError, match="already registered"):
        reg.add("two", root.upper() if os.name == "nt" else root)


def test_duplicate_names_get_unique_ids(reg, tmp_path):
    a = reg.add("same", make_app(tmp_path, "a"))
    b = reg.add("same", make_app(tmp_path, "b"))
    assert a["id"] == "same" and b["id"].startswith("same-")
    assert a["id"] != b["id"]


def test_platform_repository_refused_without_flag(reg):
    from core import config as config_mod
    platform_root = str(config_mod.AGENTIC_DIR.parent)
    with pytest.raises(RegistryError, match="platform repository"):
        reg.add("self", platform_root)
    record = reg.add("self", platform_root, allow_platform=True)
    assert record["id"] == "self"


def test_authorised_roots_enforced(reg, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    reg.authorise_root(str(allowed))
    outside = make_app(tmp_path, "outside")
    with pytest.raises(RegistryError, match="authorised roots"):
        reg.add("bad", outside)
    inside = allowed / "good-app"
    inside.mkdir()
    assert reg.add("good", str(inside))["id"] == "good"


def test_no_credentials_in_registry(reg, tmp_path):
    reg.add("clean", make_app(tmp_path))
    raw = open(reg.path, encoding="utf-8").read()
    assert "api_key" not in raw.lower()
    assert "token" not in raw.lower()
    assert "password" not in raw.lower()


# -- lifecycle -----------------------------------------------------------------------

def test_archive_and_remove_never_delete_files(reg, tmp_path):
    root = make_app(tmp_path)
    record = reg.add("keeper", root)
    reg.ensure_runtime_dirs(record["id"])
    runtime = reg.project_runtime_dir(record["id"])
    reg.archive(record["id"])
    assert reg.get(record["id"])["status"] == "archived"
    assert os.path.exists(os.path.join(root, "plan.md"))
    result = reg.remove(record["id"])
    assert "NOT deleted" in result["note"]
    assert os.path.exists(os.path.join(root, "plan.md"))
    assert os.path.isdir(runtime)          # runtime state also preserved
    with pytest.raises(RegistryError):
        reg.get(record["id"])
    # archived path can be re-registered
    assert reg.add("keeper", root)["id"]


def test_move_and_relink(reg, tmp_path):
    root = make_app(tmp_path)
    record = reg.add("mover", root)
    new_root = str(tmp_path / "apps" / "moved-app")
    os.rename(root, new_root)
    updated = reg.relink(record["id"], new_root)
    assert updated["root_path"] == os.path.abspath(new_root)
    assert updated["canonical_root_path"] == canonical(new_root)
    assert updated["id"] == record["id"]   # identity preserved


def test_immutable_fields_and_unknown_fields(reg, tmp_path):
    record = reg.add("fixed", make_app(tmp_path))
    with pytest.raises(RegistryError, match="immutable"):
        reg.update(record["id"], id="other")
    with pytest.raises(RegistryError, match="unknown fields"):
        reg.update(record["id"], nonsense=1)


# -- storage robustness -----------------------------------------------------------------

def test_corrupt_registry_preserved_and_recovered(reg, tmp_path):
    reg.add("survivor", make_app(tmp_path))
    with open(reg.path, "w", encoding="utf-8") as fh:
        fh.write("{broken json")
    data = reg.load()
    assert data["projects"] == {}          # honest empty, not a crash
    assert any(name.startswith("registry.json.corrupt-")
               for name in os.listdir(reg.home))


def test_schema_version_and_newer_schema_rejected(reg, tmp_path):
    reg.add("versioned", make_app(tmp_path))
    data = json.load(open(reg.path, encoding="utf-8"))
    assert data["schema_version"] == 1
    data["schema_version"] = 99
    json.dump(data, open(reg.path, "w", encoding="utf-8"))
    with pytest.raises(RegistryError, match="newer"):
        reg.load()


def test_runtime_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_HOME", str(tmp_path / "custom"))
    assert runtime_home() == str(tmp_path / "custom")


# -- Phase 2: state boundaries and isolation -----------------------------------------

def test_project_cfg_overlay_redirects_paths(reg, tmp_path, base_cfg):
    root = make_app(tmp_path, "overlay-app")
    record = reg.add("overlay", root)
    cfg = reg.project_cfg(base_cfg, record["id"])
    from core.config import repo_root
    from core.project import _paths
    assert str(repo_root(cfg)) == os.path.abspath(root)
    p = _paths(cfg)
    runtime = reg.project_runtime_dir("overlay")
    assert p["agentic"] == runtime
    assert p["memory"] == os.path.join(runtime, "memory")
    assert p["root"] == os.path.abspath(root)
    # base cfg untouched (deep copy)
    assert "runtime" not in base_cfg


def test_paths_without_overlay_unchanged(base_cfg):
    from core import config as config_mod
    from core.project import _paths
    p = _paths(base_cfg)
    assert p["agentic"] == str(config_mod.AGENTIC_DIR)


def test_two_projects_with_identical_filenames_isolated(reg, tmp_path,
                                                        base_cfg):
    """Same file names, same plan names — state, memory and knowledge
    stay fully separated per project."""
    from core.memsvc import get_memory
    from core.knowledge import KnowledgeVault
    a_root = make_app(tmp_path, "twin-a")
    b_root = make_app(tmp_path, "twin-b")
    a = reg.add("twin-a", a_root)
    b = reg.add("twin-b", b_root)
    cfg_a = reg.project_cfg(base_cfg, a["id"])
    cfg_b = reg.project_cfg(base_cfg, b["id"])
    mem_a = os.path.join(reg.project_runtime_dir("twin-a"), "memory")
    mem_b = os.path.join(reg.project_runtime_dir("twin-b"), "memory")
    get_memory(cfg_a, mem_a).save("constraint", "A only", "alpha fact")
    get_memory(cfg_b, mem_b).save("constraint", "B only", "beta fact")
    assert get_memory(cfg_a, mem_a).search("beta") == []
    assert get_memory(cfg_b, mem_b).search("alpha") == []
    vault_a = KnowledgeVault(cfg_a, reg.project_runtime_dir("twin-a"))
    vault_a.write_doc("note.md", "n", "note", "T", "only in A")
    vault_b = KnowledgeVault(cfg_b, reg.project_runtime_dir("twin-b"))
    assert vault_b.documents() == []


def test_spaces_in_paths(reg, tmp_path):
    root = tmp_path / "apps" / "My Cool App"
    root.mkdir(parents=True)
    (root / "plan.md").write_text("# p\n", encoding="utf-8")
    record = reg.add("spacey", str(root))
    assert "My Cool App" in record["root_path"]
    assert reg.find_by_root(str(root))["id"] == "spacey"


# -- projectops: init / doctor / legacy ----------------------------------------------

def test_project_init_idempotent_and_model_free(reg, tmp_path, base_cfg):
    from core import projectops
    root = make_app(tmp_path, "initme", git=False)
    record = reg.add("initme", root)
    result = projectops.project_init(base_cfg, reg, record["id"])
    assert result["ok"], result
    assert result["steps"]["git_init"] is True
    assert result["steps"]["initial_commit"] is True
    assert result["steps"]["plan"].endswith("plan.md")
    assert os.path.isdir(result["steps"]["runtime_dir"])
    assert reg.get("initme")["status"] == "initialised"
    # second run: nothing re-initialises, still ok
    again = projectops.project_init(base_cfg, reg, record["id"])
    assert again["ok"] and again["steps"]["git_init"] is False


def test_project_init_rejects_nested_repo_root(reg, tmp_path, base_cfg):
    from core import projectops
    outer = make_app(tmp_path, "outer")
    nested = os.path.join(outer, "sub")
    os.makedirs(nested)
    record = reg.add("nested", nested)
    with pytest.raises(RegistryError, match="another git repository"):
        projectops.project_init(base_cfg, reg, record["id"])


def test_project_doctor_reports(reg, tmp_path, base_cfg):
    from core import projectops
    root = make_app(tmp_path, "docme")
    record = reg.add("docme", root)
    report = projectops.project_doctor(base_cfg, reg, record["id"])
    assert report["ok"]
    messages = [m for _lv, m in report["checks"]]
    assert any("root exists" in m for m in messages)
    assert any("git repository" in m for m in messages)


def test_adopt_legacy_registers_platform_in_place(reg, base_cfg,
                                                  sandbox):
    from core import projectops
    record = projectops.adopt_legacy(sandbox["cfg"], reg)
    assert record["metadata"]["legacy"] is True
    cfg = projectops.project_cfg_for(sandbox["cfg"], reg, record)
    from core import config as config_mod
    # legacy state stays exactly where it already lives
    assert cfg["runtime"]["project_dir"] == str(config_mod.AGENTIC_DIR)
    # idempotent
    assert projectops.adopt_legacy(sandbox["cfg"], reg)["id"] == record["id"]


def test_integration_detection(reg, tmp_path):
    from core.projectops import detect_integrations
    root = make_app(tmp_path, "stack")
    os.makedirs(os.path.join(root, "supabase", "migrations"))
    open(os.path.join(root, "supabase", "config.toml"), "w").close()
    open(os.path.join(root, "docker-compose.yml"), "w").close()
    found = detect_integrations(root)
    assert found["docker"] and found["supabase"]
    assert found["supabase_migrations"]
