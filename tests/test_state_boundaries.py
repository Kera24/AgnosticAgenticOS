"""MP Phase 2 — state-boundary invariants: no CWD reliance in core logic,
runtime home outside the repository, packaging stays clean of it."""
import os
import re


CORE = os.path.join(os.path.dirname(__file__), "..", ".agentic")


def test_no_cwd_reliance_in_core_logic():
    """The current directory may appear ONLY in the registered-project
    convenience resolver (projectops.resolve_project)."""
    offenders = []
    for base, dirs, files in os.walk(CORE):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            text = open(path, encoding="utf-8").read()
            if re.search(r"os\.getcwd\(\)|Path\.cwd\(\)", text):
                if os.path.basename(path) != "projectops.py":
                    offenders.append(path)
    assert offenders == [], offenders


def test_default_runtime_home_outside_repository(monkeypatch):
    from core import config as config_mod
    from core.registry import runtime_home
    monkeypatch.delenv("AGENTIC_HOME", raising=False)
    home = os.path.realpath(runtime_home())
    repo = os.path.realpath(str(config_mod.AGENTIC_DIR.parent))
    assert not home.startswith(repo + os.sep)
    assert home != repo


def test_registry_and_runtime_state_never_git_tracked(tmp_path):
    """Even with AGENTIC_HOME pointed inside a repo (misconfiguration),
    package output built from git-tracked files can never include it —
    registry state is never `git add`ed by any code path."""
    import subprocess
    from core.registry import ProjectRegistry
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo),
                   capture_output=True)
    home = repo / ".agentic-os"
    registry = ProjectRegistry(home=str(home))
    app = tmp_path / "app"
    app.mkdir()
    registry.add("boundary", str(app))
    tracked = subprocess.run(["git", "ls-files"], cwd=str(repo),
                             capture_output=True, text=True).stdout
    status = subprocess.run(["git", "status", "--porcelain"],
                            cwd=str(repo), capture_output=True,
                            text=True).stdout
    assert ".agentic-os" not in tracked
    # untracked is fine (the user chose that location); staged is not
    assert not any(line.startswith(("A ", "M "))
                   for line in status.splitlines())


def test_project_state_confined_to_runtime_dir(tmp_path, base_cfg):
    """Running memory/knowledge/index for a registered project writes
    ONLY under its runtime dir — never into the application repo, never
    into another project's dir."""
    from core.registry import ProjectRegistry
    from core.memsvc import get_memory
    registry = ProjectRegistry(home=str(tmp_path / "home"))
    app = tmp_path / "clean-app"
    app.mkdir()
    (app / "plan.md").write_text("# p\n", encoding="utf-8")
    record = registry.add("clean", str(app))
    registry.ensure_runtime_dirs(record["id"])
    cfg = registry.project_cfg(base_cfg, record["id"])
    memdir = os.path.join(registry.project_runtime_dir("clean"), "memory")
    get_memory(cfg, memdir).save("constraint", "x", "y")
    app_files = {name for name in os.listdir(str(app))}
    assert app_files == {"plan.md"}        # application repo untouched
    assert os.path.exists(os.path.join(memdir, "memory.db"))
