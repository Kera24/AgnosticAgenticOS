"""OPT-IN live fixture: a real, native, single-task project cycle driven
by a real local Ollama call -- no mocked model anywhere in this file.

Skipped unless AGENTIC_LIVE_SMOKE=1, exactly like tests/test_live_smoke.py
(the existing opt-in convention in this repo) -- the default test suite
never consumes model capacity, and a local Ollama call, while free of
provider cost, still takes real wall-clock time and needs the model
actually installed. Run explicitly with:

    AGENTIC_LIVE_SMOKE=1 python -m pytest tests/test_phase3_live_ollama.py -q

This is intentionally a SMALL, single-task project (one trivial file
write) rather than the full 10-task ollama-pilot backlog -- proving the
real native-Ollama round trip (architect -> conductor -> coder, real
worktrees, real deterministic gate) without the wall-clock cost of a
full project. The actual ollama-pilot project on this machine was
exercised directly (not via pytest) as part of this phase's live
verification; see the phase report for its outcome.
"""
import os
import shutil
import tempfile

import pytest

LIVE = os.environ.get("AGENTIC_LIVE_SMOKE") == "1"
pytestmark = pytest.mark.skipif(
    not LIVE, reason="live smoke tests are opt-in (set AGENTIC_LIVE_SMOKE=1); "
                     "they need a real local Ollama model installed")


def test_live_native_ollama_single_task_project_cycle():
    from core.config import load_config
    from core import backends as backends_mod

    cfg = load_config()
    if "ollama" not in (cfg.get("backends") or {}):
        pytest.skip("no 'ollama' backend configured")
    try:
        adapter = backends_mod.build_backend(cfg, "ollama")
        if not adapter.smoke_test(os.getcwd()):
            pytest.skip("ollama smoke test failed -- no local model reachable")
    except Exception as exc:   # noqa: BLE001
        pytest.skip("ollama not reachable on this machine: %s" % exc)

    import subprocess

    from core.project import project_start, run_cycle
    import core.config as config_mod

    tmp = tempfile.mkdtemp(prefix="agentic-live-ollama-")
    try:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo,
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo,
                       check=True, capture_output=True)
        with open(os.path.join(repo, "README.md"), "w") as fh:
            fh.write("seed\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True,
                       capture_output=True)

        agentic = os.path.join(tmp, "agentic")
        agentic_src = config_mod.AGENTIC_DIR
        for sub in ("prompts", "schemas", "guardrails", "capabilities"):
            shutil.copytree(str(agentic_src / sub),
                            os.path.join(agentic, sub))
        for sub in ("memory", "queue", "runs", "goals", "worktrees"):
            os.makedirs(os.path.join(agentic, sub))

        import copy
        live_cfg = copy.deepcopy(cfg)
        live_cfg.setdefault("project", {})["repository_root"] = repo
        live_cfg.setdefault("execution", {})["worktree_enabled"] = True
        live_cfg.setdefault("routing", {})["mode"] = "simple"
        live_cfg["routing"]["primary"] = "ollama"
        live_cfg["routing"]["fallbacks"] = []
        live_cfg.setdefault("interaction", {})["mode"] = "completion_only"
        live_cfg.setdefault("scheduler", {}).setdefault(
            "continuation", {})["automatic"] = True
        live_cfg["scheduler"].setdefault("cooling", {
            "after_success_minutes": 30, "after_failure_minutes": 30,
            "minimum_minutes": 5, "maximum_minutes": 360})
        live_cfg["scheduler"].setdefault(
            "operating_window", {"enabled": False})
        live_cfg.setdefault("repair", {"maximum_attempts_per_task": 2})

        plan_path = os.path.join(repo, "plan.md")
        with open(plan_path, "w") as fh:
            fh.write("# Trivial marker file project\n\n"
                    "Create a single file `hello.txt` containing the "
                    "text `hello from agentic os`. Nothing else. No "
                    "test framework needed for this one-file project.\n")

        original_agentic_dir = config_mod.AGENTIC_DIR
        config_mod.AGENTIC_DIR = type(original_agentic_dir)(agentic)
        try:
            start = project_start(live_cfg, plan_path)
            assert start["status"] == "started", start
            result = run_cycle(live_cfg)
        finally:
            config_mod.AGENTIC_DIR = original_agentic_dir

        assert result["status"] in ("success", "complete", "failure"), result
        # the assertion that matters for THIS opt-in proof: the platform
        # actually reached and completed a real native Ollama round trip
        # (not "human_required"/"waiting_capacity"/"locked" -- i.e. it
        # never even got to invoke the model).
        assert result["status"] not in ("human_required", "waiting_capacity",
                                        "locked", "no_project")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
