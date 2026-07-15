"""Configuration precedence, machine-local config, interactive setup output,
command-execution hardening, and post-call budget containment."""
import os

import pytest
import yaml

from conftest import AGENTIC_SRC, FakeRunner, Transport, oai_body
from core import errors, execpolicy
from core.budget import Budget
from core.config import deep_merge, load_config


# 1/2. configuration precedence + machine-local configuration -----------------
def test_config_precedence_layers(tmp_path, monkeypatch):
    import core.config as config_mod
    agentic = tmp_path / "agentic"
    (agentic / "profiles").mkdir(parents=True)
    (agentic / "config.yaml").write_text(yaml.safe_dump({
        "execution": {"mode": "review", "max_changed_lines": 400},
        "routing": {"primary": "base", "fallbacks": []},
        "project": {"repository_root": ".."}}), encoding="utf-8")
    (agentic / "config.machine.yaml").write_text(yaml.safe_dump({
        "routing": {"primary": "machine"}}), encoding="utf-8")
    (agentic / "profiles" / "fast.yaml").write_text(yaml.safe_dump({
        "routing": {"primary": "profile"}}), encoding="utf-8")
    monkeypatch.setattr(config_mod, "AGENTIC_DIR", agentic)

    # machine overrides base
    cfg = load_config(path=str(agentic / "config.yaml"), env={})
    assert cfg["routing"]["primary"] == "machine"
    assert cfg["execution"]["max_changed_lines"] == 400   # base preserved
    # profile overrides machine
    cfg = load_config(path=str(agentic / "config.yaml"), env={},
                      profile="fast")
    assert cfg["routing"]["primary"] == "profile"
    # env overrides profile
    cfg = load_config(path=str(agentic / "config.yaml"),
                      env={"AGENTIC_EXECUTION_MODE": "auto"}, profile="fast")
    assert cfg["execution"]["mode"] == "auto"
    # CLI overrides everything
    cfg = load_config(path=str(agentic / "config.yaml"), env={},
                      profile="fast",
                      cli_overrides={"routing.primary": "cli-flag"})
    assert cfg["routing"]["primary"] == "cli-flag"


def test_deep_merge_does_not_mutate_base():
    base = {"a": {"b": 1, "c": 2}}
    merged = deep_merge(base, {"a": {"b": 9}})
    assert merged["a"] == {"b": 9, "c": 2}
    assert base["a"]["b"] == 1


def test_machine_config_is_gitignored():
    gitignore = (AGENTIC_SRC.parent / ".gitignore").read_text(encoding="utf-8")
    assert ".agentic/config.machine.yaml" in gitignore


# 3. interactive setup output ---------------------------------------------------
def test_setup_wizard_writes_machine_config(base_cfg, tmp_path, monkeypatch):
    import core.config as config_mod
    from core.setupwiz import run_setup
    agentic = tmp_path / "agentic"
    agentic.mkdir()
    monkeypatch.setattr(config_mod, "AGENTIC_DIR", agentic)

    # fake CLI environment: codex + ollama installed, claude/qwen absent
    def which(binary):
        return {"codex": "C:/bin/codex", "ollama": "C:/bin/ollama"}.get(binary)

    runner = FakeRunner([
        {"stdout": "codex-cli 1.2.3"},          # codex --version
        {"stdout": "Logged in using ChatGPT"},  # codex login status
        {"stdout": "ollama version 0.5"},       # ollama --version
        {"stdout": "NAME  ID  SIZE\nllama3:8b  x  4GB\nqwen3:4b  y  2GB"},
    ])
    result = run_setup(cfg=base_cfg, runner=runner, which=which, smoke=False,
                       answers=["llama3:8b",        # ollama model
                                "simple",           # routing mode
                                "codex",            # primary
                                "ollama",           # fallbacks
                                "20", "30",         # cycle, cooling
                                "completion_only",  # interaction
                                "07:00-22:00",      # operating hours
                                "no"])              # desktop notifications
    assert result["ok"]
    machine = yaml.safe_load(open(result["path"], encoding="utf-8"))
    assert machine["routing"] == {"mode": "simple", "primary": "codex",
                                  "fallbacks": ["ollama"]}
    assert machine["backends"]["ollama"]["model"] == "llama3:8b"
    assert machine["backends"]["codex"]["kind"] == "codex"
    assert machine["interaction"]["mode"] == "completion_only"
    assert machine["scheduler"]["operating_window"]["start"] == "07:00"
    assert machine["notifications"]["desktop"] is False
    text = "\n".join(result["output"])
    assert "codex" in text and "auth=ok" in text
    # no credential content anywhere in the wizard output or file
    assert "sk-" not in text and "sk-" not in open(result["path"]).read()


# 40 (part). command-execution hardening -------------------------------------------
def test_string_commands_run_without_shell():
    result = execpolicy.run_command("python -c \"print('hi')\"", cwd=".",
                                    timeout=30)
    assert result["shell"] is False
    assert result["exit_code"] == 0
    assert "hi" in result["stdout"]
    assert result["argv"][0] == "python"


def test_shell_requires_explicit_admin_flag_and_never_for_models():
    with pytest.raises(errors.PolicyError):
        execpolicy.run_command("echo hi", cwd=".", timeout=10,
                               shell_required=True, source="model")
    with pytest.raises(errors.PolicyError):
        execpolicy.run_command(["echo", "hi"], cwd=".", timeout=10,
                               shell_required=True)   # arrays never shell


def test_model_commands_must_match_allowlist_verbatim():
    assert execpolicy.run_allowlisted("python -c \"1/0\" ; rm -rf /",
                                      ["python -m pytest -q"], ".", 10) is None
    run = execpolicy.run_allowlisted("python -c \"print(1)\"",
                                     ["python -c \"print(1)\""], ".", 30)
    assert run is not None and run["exit_code"] == 0 and run["shell"] is False


def test_no_shell_true_outside_execpolicy():
    """Repo-wide guard: shell=True may exist only inside execpolicy."""
    import pathlib
    offenders = []
    for path in pathlib.Path(str(AGENTIC_SRC)).rglob("*.py"):
        if path.name == "execpolicy.py":
            continue
        if "shell=True" in path.read_text(encoding="utf-8", errors="replace"):
            offenders.append(str(path))
    assert offenders == []


# 39. post-call budget exceptions safely contained -----------------------------------
def test_post_call_budget_exhaustion_does_not_crash(base_cfg, tmp_path):
    base_cfg["budget"]["daily_limit_usd"] = 0.001
    base_cfg["pricing"] = {"mock": {"default": {"input": 1000.0,
                                                "output": 1000.0}}}
    base_cfg["providers"]["mock"]["cost_free"] = False
    budget = Budget(base_cfg, str(tmp_path / "m"), "r")
    from core.invoke import invoke_model
    # the call itself completes and its result is preserved...
    resp = invoke_model(base_cfg, "triage", "x", budget=budget,
                        transport=Transport([(200, oai_body("hello",
                                                            in_tok=100000,
                                                            out_tok=100000))]))
    assert resp["ok"] is True
    assert resp["content"] == "hello"
    assert budget.exhausted_reason is not None
    # ...and the NEXT call is stopped safely instead of crashing anything
    resp2 = invoke_model(base_cfg, "triage", "x", budget=budget,
                         transport=Transport([(200, oai_body("nope"))]))
    assert resp2["ok"] is False
    assert resp2["error"]["kind"] == "budget_exceeded"
