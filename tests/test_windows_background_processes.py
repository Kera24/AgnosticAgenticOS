"""Windows child-processes stay background-only while output is captured."""
import subprocess
from types import SimpleNamespace

from core import execpolicy, supervisor


def test_windows_background_creationflags_adds_no_window(monkeypatch):
    monkeypatch.setattr(
        execpolicy.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    flags = execpolicy.windows_background_creationflags(
        existing=0x00000200, platform="nt")

    assert flags & 0x08000000
    assert flags & 0x00000200


def test_windows_background_creationflags_can_be_shown_for_debugging():
    assert execpolicy.windows_background_creationflags(
        existing=123, platform="nt", show_child_windows=True) == 123
    assert execpolicy.windows_background_creationflags(
        existing=123, platform="posix") == 123


def test_run_command_passes_background_creationflags(monkeypatch, tmp_path):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(execpolicy, "windows_background_creationflags",
                        lambda existing=0, **_kw: 456)
    monkeypatch.setattr(execpolicy.subprocess, "run", fake_run)

    result = execpolicy.run_command(
        [subprocess.list2cmdline(["python"])], str(tmp_path), timeout=5)

    assert result["exit_code"] == 0
    assert captured["creationflags"] == 456
    assert captured["capture_output"] is True


def test_supervisor_combines_process_group_with_background_flag(
        monkeypatch, tmp_path):
    captured = {}
    real_popen = subprocess.Popen

    monkeypatch.setattr(supervisor, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        supervisor.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)

    def background(existing=0, **_kw):
        captured["existing"] = existing
        return 0  # valid on the non-Windows host running this unit test

    def spy_popen(*args, **kwargs):
        captured["creationflags"] = kwargs.get("creationflags")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(
        execpolicy, "windows_background_creationflags", background)
    monkeypatch.setattr(supervisor.subprocess, "Popen", spy_popen)

    result = supervisor.run_supervised(
        [subprocess.sys.executable, "-c", "pass"],
        cwd=str(tmp_path), timeout=5)

    assert result["exit_code"] == 0
    assert captured["existing"] == 512
    assert captured["creationflags"] == 0
