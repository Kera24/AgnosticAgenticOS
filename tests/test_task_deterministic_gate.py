"""Canonical task-specific deterministic gate regression tests."""
from core import gate


def _result(argv, exit_code=0):
    return {
        "argv": argv,
        "stdout": "ok" if exit_code == 0 else "",
        "stderr": "" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "timed_out": False,
    }


def test_required_task_check_runs_alongside_autodetection(
        tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _result([command])

    monkeypatch.setattr(gate.execpolicy, "run_command", fake_run)
    result = gate.run_checks(
        {"verification": {"commands": "auto"}},
        str(tmp_path),
        required_commands=[
            "node -e \"require('./dist/tests/t5-ui')\"",
        ])

    assert result["ok"] is True
    assert calls == ["node -e \"require('./dist/tests/t5-ui')\""]
    assert result["results"][0]["name"] == "task-deterministic-1"
    assert result["results"][0]["passed"] is True


def test_required_task_fallback_chain_never_uses_shell(
        tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get("shell_required")))
        return _result([command], 1 if command == "npm test" else 0)

    monkeypatch.setattr(gate.execpolicy, "run_command", fake_run)
    result = gate.run_checks(
        {"verification": {"commands": "auto"}},
        str(tmp_path),
        required_commands=["npm test || npx vitest --run"])

    assert result["ok"] is True
    assert calls == [("npm test", False), ("npx vitest --run", False)]
    assert result["results"][0]["attempted_alternatives"] == [
        ["npm test"], ["npx vitest --run"]]


def test_explicit_admin_checks_remain_authoritative(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _result([command])

    monkeypatch.setattr(gate.execpolicy, "run_command", fake_run)
    result = gate.run_checks(
        {"verification": {"commands": [{
            "name": "offline-substitute",
            "command": "python -c \"print('safe')\"",
            "mandatory": True,
        }]}},
        str(tmp_path),
        required_commands=["npm run build"])

    assert result["ok"] is True
    assert calls == ["python -c \"print('safe')\""]
