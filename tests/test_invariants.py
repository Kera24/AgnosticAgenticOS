"""Repository-wide security invariants (spec: SECURITY REQUIREMENTS).
These scan the actual source tree and exercise the enforcement points, so
a regression anywhere trips them."""
import os
import re

import pytest

from core import errors, execpolicy

CORE = os.path.join(os.path.dirname(__file__), "..", ".agentic")


def source_files(*subdirs):
    for sub in subdirs:
        for base, dirs, files in os.walk(os.path.join(CORE, sub)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith(".py"):
                    yield os.path.join(base, name)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# 1–2. model commands never get a shell; shell only for admin strings ---------

def test_model_commands_never_shell():
    with pytest.raises(errors.PolicyError):
        execpolicy.run_command("echo hi", ".", 5, shell_required=True,
                               source="model")


def test_shell_true_only_inside_execpolicy():
    offenders = []
    for path in source_files("core", "providers", "ui"):
        if os.path.basename(path) == "execpolicy.py":
            continue
        if re.search(r"shell\s*=\s*True", read(path)):
            offenders.append(path)
    assert offenders == [], "shell=True outside execpolicy: %s" % offenders


def test_no_os_system_or_eval_anywhere():
    offenders = []
    for path in source_files("core", "providers", "ui"):
        text = read(path)
        if re.search(r"\bos\.system\s*\(", text) or \
                re.search(r"(?<![\w.])eval\s*\(", text):
            offenders.append(path)
    assert offenders == []


# 3. model content cannot change the allowlist --------------------------------

def test_allowlist_is_config_only():
    result = execpolicy.run_allowlisted(
        "python -m pytest -q", ["npm test"], ".", 5)
    assert result is None                     # not allowlisted -> skipped
    # a model asking to extend the list is just another non-listed command
    assert execpolicy.run_allowlisted(
        "safe_commands.append", ["npm test"], ".", 5) is None


# 4. paths cannot escape the workspace ------------------------------------------

def test_workspace_confinement_everywhere(tmp_path):
    from core import gitops
    with pytest.raises(errors.PolicyError):
        gitops.safe_join(str(tmp_path), "../outside.txt")
    from core.knowledge import KnowledgeVault
    vault = KnowledgeVault({"project": {"name": "t"}}, str(tmp_path))
    with pytest.raises(ValueError):
        vault.path("../../etc/passwd")
    from core.skillreg import SkillError, SkillRegistry
    registry = SkillRegistry({"project": {"name": "t"}}, str(tmp_path))
    os.makedirs(tmp_path / "skills" / "builtin" / "s1", exist_ok=True)
    (tmp_path / "skills" / "builtin" / "s1" / "SKILL.md").write_text("x")
    registry.ensure_builtins()
    with pytest.raises(SkillError):
        registry.load_file("s1", "..\\..\\config.machine.yaml")


# 6/7 covered by broker/routing tests; assert the code constants hold ---------

def test_auth_and_refusal_never_fallback_kinds():
    assert "auth" in errors.NO_FALLBACK_KINDS
    assert "auth" not in errors.FALLBACK_KINDS
    # refusals never fall back either: invoke_backend returns immediately
    # on refusal (tested in test_cli_backends/test_routing); the kind is
    # deliberately not retryable/fallback-able
    assert "refusal" not in errors.FALLBACK_KINDS
    assert "refusal" not in errors.RETRYABLE_KINDS


# 8. push/merge/deploy absent from autonomous execution -------------------------

FORBIDDEN_GIT = re.compile(
    r"run_git\(\s*\[\s*[\"'](push|merge|pull)[\"']", re.IGNORECASE)
FORBIDDEN_WORDS = re.compile(
    r"[\"'](git push|git merge|deploy --prod|npm publish|twine upload)[\"']")


def test_no_push_merge_deploy_in_autonomous_code():
    offenders = []
    for path in source_files("core", "providers", "ui"):
        text = read(path)
        if FORBIDDEN_GIT.search(text) or FORBIDDEN_WORDS.search(text):
            offenders.append(path)
    assert offenders == []


# 9. runtime state and machine config excluded from packages --------------------

def test_package_excludes_runtime_and_machine_state(tmp_path):
    import subprocess
    import sys
    import zipfile
    root = os.path.join(os.path.dirname(__file__), "..")
    out = str(tmp_path / "dist.zip")
    proc = subprocess.run(
        [sys.executable, os.path.join(root, ".agentic", "run"), "package",
         out], cwd=root, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    names = zipfile.ZipFile(out).namelist()
    forbidden = [n for n in names if
                 n.startswith((".agentic/runs/", ".agentic/worktrees/"))
                 or n == ".agentic/config.machine.yaml"
                 or n.endswith((".env", "memory.db", "usage.tsv",
                                "capacity.tsv", "scheduler.json",
                                "context-ledger.jsonl",
                                "routing-decisions.jsonl"))
                 or "/code-index/" in n]
    assert forbidden == [], forbidden
    # pristine memory templates ship instead of local ledgers
    assert ".agentic/memory/STATE.md" in names


# 12/13. integrations pinned & optional; no cloud sync --------------------------

def test_no_network_calls_outside_provider_transport():
    """urllib/requests/socket connections live only in providers/base.py
    (the shared transport) and the loopback UI server."""
    allowed = {"base.py", "serve.py"}
    offenders = []
    for path in source_files("core", "providers", "ui"):
        if os.path.basename(path) in allowed:
            continue
        text = read(path)
        if re.search(r"urllib\.request\.urlopen|requests\.(get|post)|"
                     r"http\.client\.HTTPConnection", text):
            offenders.append(path)
    assert offenders == []


# 14. every autonomous action is auditable -------------------------------------

def test_cycle_actions_logged(sandbox):
    from conftest import (Clock, FakeCaller, project_cfg, proj_order,
                          seed_project, simple_task, verifier_out,
                          worker_out)
    from core.project import run_cycle
    cfg = project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    caller = FakeCaller({"conductor": proj_order(task),
                         "coder": worker_out(),
                         "qa": verifier_out("pass"),
                         "security": {"verdict": "pass", "concerns": [],
                                      "reason": "clean"}})
    result = run_cycle(cfg, caller=caller, clock=Clock())
    assert result["status"] == "success"
    log_path = sandbox["agentic"] / "memory" / "decisions.jsonl"
    text = log_path.read_text(encoding="utf-8")
    for event in ("capacity_decision", "qa_review", "cycle_finished"):
        assert event in text
