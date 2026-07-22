"""Deterministic verification gate — the final technical vote. No model can
override it. Checks come from config (verification.commands) or are
auto-detected from the repository. A pre-existing baseline failure is not a
regression, but it is reported honestly; a NEW failure always fails the gate.
"""
import datetime as _dt
import json
import os

from . import execpolicy

# Deterministic-check classification (bootstrap fix): every check result is
# tagged with exactly one of these kinds so callers can tell "a real test
# suite ran and passed" apart from every other flavour of deterministic
# evidence (build/lint/typecheck/syntax/structural/security/isolation).
CHECK_KINDS = ("test_suite", "build", "typecheck", "lint", "syntax",
              "structural", "security", "project_isolation")


def classify_check_kind(name, command):
    """Best-effort classification of a configured/detected check. An
    explicit `kind` on the check dict always wins (see `run_checks`); this
    is only the fallback for checks that don't declare one."""
    name_l = (name or "").lower()
    cmd_l = str(command or "").lower()
    if "lint" in name_l:
        return "lint"
    if "typecheck" in name_l or "tsc" in cmd_l or "mypy" in cmd_l:
        return "typecheck"
    if "build" in name_l:
        return "build"
    return "test_suite"   # the historical default: verification.commands
                          # has always meant "the test suite" unless labeled


def detect_commands(repo_root):
    """Best-effort autodetection of repository checks."""
    commands = []
    exists = lambda *p: os.path.exists(os.path.join(repo_root, *p))
    if (exists("pyproject.toml") or exists("pytest.ini") or exists("setup.cfg")
            or exists("tests")):
        commands.append({"name": "pytest", "command": "python -m pytest -q",
                         "mandatory": True, "kind": "test_suite"})
    if exists("package.json"):
        try:
            with open(os.path.join(repo_root, "package.json"), encoding="utf-8") as fh:
                scripts = (json.load(fh).get("scripts") or {})
        except ValueError:
            scripts = {}
        script_kind = {"lint": "lint", "test": "test_suite", "build": "build"}
        for script in ("lint", "test", "build"):
            if script in scripts:
                commands.append({"name": "npm-%s" % script,
                                 "command": "npm run %s --silent" % script,
                                 "mandatory": script != "lint",
                                 "kind": script_kind[script]})
    if exists("Cargo.toml"):
        commands.append({"name": "cargo-test", "command": "cargo test --quiet",
                         "mandatory": True, "kind": "test_suite"})
    if exists("go.mod"):
        commands.append({"name": "go-test", "command": "go test ./...",
                         "mandatory": True, "kind": "test_suite"})
    return commands


def resolve_commands(cfg, repo_root):
    configured = (cfg.get("verification", {}) or {}).get("commands", "auto")
    if configured == "auto" or configured is None:
        return detect_commands(repo_root), True
    return [dict(c) for c in configured], False


def baseline_path(agentic_dir):
    return os.path.join(agentic_dir, "memory", "baseline.json")


def load_baseline(agentic_dir):
    path = baseline_path(agentic_dir)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def save_baseline(agentic_dir, results):
    data = {"recorded_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "checks": {r["name"]: r["passed"] for r in results}}
    with open(baseline_path(agentic_dir), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return data


def run_checks(cfg, workdir, log_dir=None, timeout=None):
    """Run every configured check in workdir. Mandatory checks are never
    skipped; a missing log_dir only skips log persistence, not checks.

    ZERO configured/detected checks is a BLOCKING failure (`ok: false`,
    `no_checks: true`): a repository without deterministic verification can
    never pass the gate, and no AI verdict may convert that into success."""
    commands, auto = resolve_commands(cfg, workdir)
    if not commands:
        return {"ok": False, "auto_detected": auto, "results": [],
                "no_checks": True, "tests": "not_configured_yet",
                "reason": "no deterministic checks configured or detected; "
                          "configure verification.commands"}
    timeout = timeout or int(cfg.get("execution", {})
                             .get("command_timeout_seconds", 900))
    fail_fast = bool((cfg.get("verification", {}) or {}).get("fail_fast", True))
    results, ok = [], True
    for check in commands:
        name = check["name"]
        mandatory = bool(check.get("mandatory", True))
        kind = check.get("kind") or classify_check_kind(name, check["command"])
        record = {"name": name, "command": check["command"],
                  "mandatory": mandatory, "passed": False, "exit_code": None,
                  "detail": "", "kind": kind}
        run = execpolicy.run_command(
            check["command"], cwd=workdir, timeout=timeout,
            shell_required=bool(check.get("shell_required", False)),
            source="config")
        record["exit_code"] = run["exit_code"]
        record["passed"] = run["exit_code"] == 0 and not run["timed_out"]
        output = run["stdout"] + run["stderr"]
        record["detail"] = ("timed out after %ss" % timeout if run["timed_out"]
                            else output[-400:].strip())
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, name + ".log"), "w",
                      encoding="utf-8", errors="replace") as fh:
                fh.write("$ %s\nexit: %s\n\n%s" % (run["argv"],
                                                   record["exit_code"], output))
        results.append(record)
        if mandatory and not record["passed"]:
            ok = False
            if fail_fast:
                break
    test_suite_results = [r for r in results if r["kind"] == "test_suite"]
    tests = ("passed" if test_suite_results and all(
                r["passed"] for r in test_suite_results)
             else "failed" if test_suite_results else "not_configured_yet")
    return {"ok": ok, "auto_detected": auto, "results": results,
            "no_checks": False, "tests": tests}


def evaluate_against_baseline(agentic_dir, gate):
    """Gate verdict considering the recorded baseline: pre-existing failures
    are tolerated (flagged), new failures are regressions."""
    baseline = load_baseline(agentic_dir)
    known = (baseline or {}).get("checks", {})
    if gate.get("no_checks"):
        return {"ok": False, "regressions": ["no-deterministic-checks-configured"],
                "known_failing": [], "fully_healthy": False,
                "baseline_recorded": baseline is not None}
    regressions, tolerated = [], []
    for r in gate["results"]:
        if r["mandatory"] and not r["passed"]:
            if known.get(r["name"]) is False:
                tolerated.append(r["name"])
            else:
                regressions.append(r["name"])
    healthy = all(r["passed"] for r in gate["results"])
    return {"ok": not regressions, "regressions": regressions,
            "known_failing": tolerated, "fully_healthy": healthy,
            "baseline_recorded": baseline is not None}
