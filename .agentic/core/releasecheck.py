"""Repeatable Agentic OS release-readiness gate.

A release is ready only when the platform's own deterministic checks pass and
multiple independently completed application projects retain passing final
audits. The report is machine-readable and persisted in the runtime home.
"""
import datetime as _dt
import json
import os
import sys

from . import config as config_mod
from . import execpolicy, projstate
from .registry import ProjectRegistry


def _tail(text, limit=2000):
    value = str(text or "")
    return value[-limit:]


def acceptance_project_results(registry, minimum_projects=2):
    projects = []
    for record in registry.list(include_archived=False):
        runtime_dir = registry.project_runtime_dir(record["id"])
        audit = projstate.read_yaml(runtime_dir, "final-audit.yaml", {}) or {}
        checks = audit.get("checks") or {}
        final_review = audit.get("final_review") or {}
        complete = bool(audit.get("complete"))
        checks_pass = bool(checks) and all(bool(v) for v in checks.values())
        review_pass = final_review.get("verdict") == "pass"
        passed = complete and checks_pass and review_pass
        projects.append({
            "id": record["id"],
            "passed": passed,
            "audit_complete": complete,
            "checks_pass": checks_pass,
            "final_review": final_review.get("verdict"),
            "failed_checks": sorted(k for k, value in checks.items()
                                    if not value),
        })
    passing = [p for p in projects if p["passed"]]
    return {
        "minimum_required": int(minimum_projects),
        "passing_count": len(passing),
        "passed": len(passing) >= int(minimum_projects),
        "projects": projects,
    }


def _run_platform_check(name, command, cwd, timeout, runner):
    result = runner(command, cwd=cwd, timeout=timeout, source="config")
    passed = result.get("exit_code") == 0 and not result.get("timed_out")
    return {
        "name": name,
        "command": command,
        "cwd": str(cwd),
        "passed": passed,
        "exit_code": result.get("exit_code"),
        "timed_out": bool(result.get("timed_out")),
        "duration_seconds": result.get("duration_seconds"),
        "detail": _tail((result.get("stdout") or "") +
                        ("\n" + result.get("stderr")
                         if result.get("stderr") else "")),
    }


def run_release_check(cfg, minimum_projects=2, registry=None, runner=None):
    registry = registry or ProjectRegistry()
    runner = runner or execpolicy.run_command
    root = str(config_mod.repo_root(cfg))
    platform_checks = [
        _run_platform_check(
            "python-tests", [sys.executable, "-m", "pytest", "tests", "-q"],
            root, 1200, runner),
        _run_platform_check(
            "frontend-build", ["npm", "run", "build"],
            os.path.join(root, "ui"), 900, runner),
    ]
    acceptance = acceptance_project_results(
        registry, minimum_projects=minimum_projects)
    ready = all(item["passed"] for item in platform_checks) and         acceptance["passed"]
    report = {
        "checked_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "ready": ready,
        "platform_checks": platform_checks,
        "acceptance_projects": acceptance,
    }
    os.makedirs(registry.home, exist_ok=True)
    path = os.path.join(registry.home, "release-readiness.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    os.replace(tmp, path)
    report["report_path"] = path
    return report
