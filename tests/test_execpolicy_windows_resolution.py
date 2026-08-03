"""Native-Windows executable resolution without shell expansion."""

from core import execpolicy


def test_resolves_bare_npm_to_cmd_without_changing_arguments():
    calls = []

    def which(command, path=None):
        calls.append((command, path))
        return r"C:\Users\Test\AppData\Roaming\npm\npm.CMD"

    env = {"PATH": r"C:\Windows;C:\Users\Test\AppData\Roaming\npm"}
    resolved = execpolicy.resolve_argv_executable(
        ["npm", "run", "test", "--silent"], env=env, platform="nt",
        which=which)

    assert resolved == [
        r"C:\Users\Test\AppData\Roaming\npm\npm.CMD",
        "run", "test", "--silent"]
    assert calls == [("npm", env["PATH"])]


def test_explicit_windows_executable_path_is_not_rewritten():
    original = [r"C:\tools\npm.cmd", "run", "test"]
    resolved = execpolicy.resolve_argv_executable(
        original, platform="nt",
        which=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("which must not run for explicit paths")))
    assert resolved == original


def test_non_windows_command_is_unchanged():
    original = ["npm", "run", "test"]
    resolved = execpolicy.resolve_argv_executable(
        original, platform="posix",
        which=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("which must not run off Windows")))
    assert resolved == original


def test_missing_windows_command_keeps_normal_exit_127_path():
    original = ["missing-tool", "--version"]
    resolved = execpolicy.resolve_argv_executable(
        original, env={"PATH": ""}, platform="nt",
        which=lambda command, path=None: None)
    assert resolved == original
