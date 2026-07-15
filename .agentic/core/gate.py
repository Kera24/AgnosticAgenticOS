"""Deterministic verification gate — the final technical vote. No model can
override it. Checks come from config (verification.commands) or are
auto-detected from the repository. A pre-existing baseline failure is not a
regression, but it is reported honestly; a NEW failure always fails the gate.
"""
import datetime as _dt
import json
import os
import subprocess


def detect_commands(repo_root):
    """Best-effort autodetection of repository checks."""
    commands = []
    exists = lambda *p: os.path.exists(os.path.join(repo_root, *p))
    if (exists("pyproject.toml") or exists("pytest.ini") or exists("setup.cfg")
            or exists("tests")):
        commands.append({"name": "pytest", "command": "python -m pytest -q",
                         "mandatory": True})
    if exists("package.json"):
        try:
            with open(os.path.join(repo_root, "package.json"), encoding="utf-8") as fh:
                scripts = (json.load(fh).get("scripts") or {})
        except ValueError:
            scripts = {}
        for script in ("lint", "test", "build"):
            if script in scripts:
                commands.append({"name": "npm-%s" % script,
                                 "command": "npm run %s --silent" % script,
                                 "mandatory": script != "lint"})
    if exists("Cargo.toml"):
        commands.append({"name": "cargo-test", "command": "cargo test --quiet",
                         "mandatory": True})
    if exists("go.mod"):
        commands.append({"name": "go-test", "command": "go test ./...",
                         "mandatory": True})
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
    skipped; a missing log_dir only skips log persistence, not checks."""
    commands, auto = resolve_commands(cfg, workdir)
    timeout = timeout or int(cfg.get("execution", {})
                             .get("command_timeout_seconds", 900))
    fail_fast = bool((cfg.get("verification", {}) or {}).get("fail_fast", True))
    results, ok = [], True
    for check in commands:
        name = check["name"]
        mandatory = bool(check.get("mandatory", True))
        record = {"name": name, "command": check["command"],
                  "mandatory": mandatory, "passed": False, "exit_code": None,
                  "detail": ""}
        try:
            proc = subprocess.run(check["command"], shell=True, cwd=workdir,
                                  capture_output=True, text=True,
                                  timeout=timeout)
            record["exit_code"] = proc.returncode
            record["passed"] = proc.returncode == 0
            output = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            record["detail"] = "timed out after %ss" % timeout
            output = record["detail"]
        record["detail"] = record["detail"] or output[-400:].strip()
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, name + ".log"), "w",
                      encoding="utf-8", errors="replace") as fh:
                fh.write("$ %s\nexit: %s\n\n%s" % (check["command"],
                                                   record["exit_code"], output))
        results.append(record)
        if mandatory and not record["passed"]:
            ok = False
            if fail_fast:
                break
    return {"ok": ok, "auto_detected": auto, "results": results,
            "no_checks": not commands}


def evaluate_against_baseline(agentic_dir, gate):
    """Gate verdict considering the recorded baseline: pre-existing failures
    are tolerated (flagged), new failures are regressions."""
    baseline = load_baseline(agentic_dir)
    known = (baseline or {}).get("checks", {})
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
