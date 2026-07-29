"""Release readiness combines platform checks with acceptance projects."""
import json
import os

from core import releasecheck


class FakeRegistry:
    def __init__(self, home, records):
        self.home = str(home)
        self._records = records

    def list(self, include_archived=False):
        return list(self._records)

    def project_runtime_dir(self, project_id):
        return os.path.join(self.home, "projects", project_id)


def _audit(home, project_id, *, complete=True, failed=False,
           verdict="pass"):
    project = home / "projects" / project_id / "project"
    project.mkdir(parents=True)
    (project / "final-audit.yaml").write_text(json.dumps({
        "complete": complete,
        "checks": {
            "backlog_complete": complete,
            "deterministic_checks_pass": not failed,
            "final_independent_review": verdict == "pass",
        },
        "final_review": {"verdict": verdict},
    }), encoding="utf-8")


def test_acceptance_projects_require_two_independent_passing_audits(tmp_path):
    _audit(tmp_path, "one")
    _audit(tmp_path, "two")
    _audit(tmp_path, "broken", failed=True, verdict="fail")
    registry = FakeRegistry(tmp_path, [
        {"id": "one"}, {"id": "two"}, {"id": "broken"}])

    result = releasecheck.acceptance_project_results(registry)

    assert result["passed"] is True
    assert result["passing_count"] == 2
    broken = next(p for p in result["projects"] if p["id"] == "broken")
    assert broken["passed"] is False
    assert broken["failed_checks"] == [
        "deterministic_checks_pass", "final_independent_review"]


def test_release_check_runs_platform_gates_and_persists_report(
        tmp_path, monkeypatch):
    _audit(tmp_path, "one")
    _audit(tmp_path, "two")
    registry = FakeRegistry(tmp_path, [{"id": "one"}, {"id": "two"}])
    root = tmp_path / "platform"
    (root / "ui").mkdir(parents=True)
    monkeypatch.setattr(
        releasecheck.config_mod, "repo_root", lambda cfg: root)
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs["cwd"]))
        return {"exit_code": 0, "timed_out": False,
                "duration_seconds": 0.1, "stdout": "ok", "stderr": ""}

    report = releasecheck.run_release_check(
        {}, registry=registry, runner=runner)

    assert report["ready"] is True
    assert [item["name"] for item in report["platform_checks"]] == [
        "python-tests", "frontend-build"]
    assert calls[0][0][1:4] == ["-m", "pytest", "tests"]
    assert calls[1] == (["npm", "run", "build"], str(root / "ui"))
    assert json.loads(
        (tmp_path / "release-readiness.json").read_text(
            encoding="utf-8"))["ready"] is True


def test_release_check_fails_when_platform_or_project_gate_fails(
        tmp_path, monkeypatch):
    _audit(tmp_path, "only-one")
    registry = FakeRegistry(tmp_path, [{"id": "only-one"}])
    root = tmp_path / "platform"
    (root / "ui").mkdir(parents=True)
    monkeypatch.setattr(
        releasecheck.config_mod, "repo_root", lambda cfg: root)

    def runner(command, **kwargs):
        failed = command[0] == "npm"
        return {"exit_code": 1 if failed else 0, "timed_out": False,
                "duration_seconds": 0.1, "stdout": "", "stderr": "bad"}

    report = releasecheck.run_release_check(
        {}, registry=registry, runner=runner)

    assert report["ready"] is False
    assert report["acceptance_projects"]["passed"] is False
    assert report["platform_checks"][1]["passed"] is False
