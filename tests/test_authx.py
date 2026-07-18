"""MP Phase 8 — authentication detection: Claude status parsing, Qwen
honesty, Ollama separation, verification gating, no secret exposure."""
import json

from conftest import FakeRunner
from core.authx import (backend_auth_report, claude_auth_detail,
                        ollama_auth_detail, qwen_auth_detail,
                        read_verification, record_verification)

WHICH = lambda b: "C:/tools/%s.exe" % b        # noqa: E731
NO_WHICH = lambda b: None                      # noqa: E731


# -- claude -------------------------------------------------------------------------

def test_claude_logged_in_json():
    runner = FakeRunner([{"exit_code": 0, "stdout": json.dumps(
        {"loggedIn": True, "authMethod": "claude.ai subscription"})}])
    report = claude_auth_detail(runner=runner, which=WHICH, env={})
    assert report["state"] == "authenticated"
    assert report["method"] == "claude.ai subscription"
    assert report["autonomous_ready"]
    assert runner.calls[0]["argv"] == ["claude", "auth", "status"]


def test_claude_logged_out_json_and_text():
    runner = FakeRunner([{"exit_code": 1, "stdout": json.dumps(
        {"loggedIn": False})}])
    report = claude_auth_detail(runner=runner, which=WHICH, env={})
    assert report["state"] == "not_authenticated"
    assert "claude auth login" in report["instructions"]
    runner2 = FakeRunner([{"exit_code": 1,
                           "stdout": "Not logged in. Run claude auth "
                                     "login."}])
    report2 = claude_auth_detail(runner=runner2, which=WHICH, env={})
    assert report2["state"] == "not_authenticated"


def test_claude_text_fallback_logged_in():
    runner = FakeRunner([{"exit_code": 0,
                          "stdout": "Logged in via claude.ai "
                                    "subscription (user@example.com)"}])
    report = claude_auth_detail(runner=runner, which=WHICH, env={})
    assert report["state"] == "authenticated"
    assert report["method"] == "subscription"


def test_claude_expired():
    runner = FakeRunner([{"exit_code": 1,
                          "stdout": "Session expired; log in again"}])
    report = claude_auth_detail(runner=runner, which=WHICH, env={})
    assert report["state"] == "expired"


def test_claude_old_version_without_status_command():
    runner = FakeRunner([{"exit_code": 1,
                          "stderr": "error: unknown command 'auth'"}])
    report = claude_auth_detail(runner=runner, which=WHICH, env={})
    assert report["state"] == "probe_failed"
    assert "update" in report["instructions"]


def test_claude_missing_executable():
    report = claude_auth_detail(runner=FakeRunner([]), which=NO_WHICH,
                                env={})
    assert report["state"] == "executable_missing"


def test_claude_credential_conflict_without_printing_value():
    runner = FakeRunner([{"exit_code": 0, "stdout": json.dumps(
        {"loggedIn": True, "authMethod": "subscription"})}])
    secret = "sk-ant-" + "x" * 30
    report = claude_auth_detail(runner=runner, which=WHICH,
                                env={"ANTHROPIC_API_KEY": secret})
    assert report["state"] == "conflicting_credentials"
    assert "ANTHROPIC_API_KEY" in report["credential_conflict"]
    assert secret not in json.dumps(report)
    # subscription auth alone (no env key) needs no API key
    report2 = claude_auth_detail(runner=FakeRunner([
        {"exit_code": 0, "stdout": json.dumps({"loggedIn": True})}]),
        which=WHICH, env={})
    assert report2["state"] == "authenticated"


# -- qwen ---------------------------------------------------------------------------

def test_qwen_missing_points_to_ollama():
    report = qwen_auth_detail(runner=FakeRunner([]), which=NO_WHICH)
    assert report["state"] == "executable_missing"
    assert "Ollama" in report["instructions"]


def test_qwen_installed_but_unverified(tmp_path):
    runner = FakeRunner([{"exit_code": 0, "stdout": "0.9.1"}])
    report = qwen_auth_detail(runner=runner, which=WHICH,
                              memory_dir=str(tmp_path),
                              home=str(tmp_path))
    assert report["state"] == "unverified"
    assert not report["autonomous_ready"]
    assert "/auth" in report["instructions"]
    assert "/doctor" in report["instructions"]
    assert "NOT authentication" in report["detail"]


def test_qwen_config_alone_is_not_authentication(tmp_path):
    (tmp_path / ".qwen").mkdir()
    (tmp_path / ".qwen" / "settings.json").write_text("{}",
                                                      encoding="utf-8")
    runner = FakeRunner([{"exit_code": 0, "stdout": "0.9.1"}])
    report = qwen_auth_detail(runner=runner, which=WHICH,
                              memory_dir=str(tmp_path / "mem"),
                              home=str(tmp_path))
    assert report["state"] == "unverified"
    assert "configuration found" in report["detail"]


def test_qwen_verified_by_smoke_test(tmp_path):
    memdir = str(tmp_path / "mem")
    record_verification(memdir, "qwen", True, "smoke passed")
    runner = FakeRunner([{"exit_code": 0, "stdout": "0.9.1"}])
    report = qwen_auth_detail(runner=runner, which=WHICH,
                              memory_dir=memdir, home=str(tmp_path))
    assert report["state"] == "authenticated"
    assert report["autonomous_ready"]


# -- ollama (separate backend, local auth) -------------------------------------------

def test_ollama_qwen_reported_separately():
    runner = FakeRunner([
        {"exit_code": 0, "stdout": "ollama version 0.32.0"},
        {"exit_code": 0, "stdout": "NAME  SIZE\nqwen3.5:latest  4GB\n"}])
    report = ollama_auth_detail(runner=runner, which=WHICH)
    assert report["backend"] == "ollama"
    assert report["state"] == "local_ok"
    assert "qwen3.5" in report["detail"]
    assert report["autonomous_ready"]


# -- routing gate --------------------------------------------------------------------

def test_unverified_qwen_cli_excluded_from_routing(tmp_path):
    from core.routing import capability_chain
    cfg = {"project": {"name": "t"},
           "backends": {
               "qwen": {"type": "cli", "kind": "configured",
                        "binary": "qwen"},
               "ollama": {"type": "local", "model": "qwen3.5:latest"}},
           "routing": {"mode": "capability", "agents": {}}}
    memdir = str(tmp_path / "mem")
    chain = capability_chain(cfg, "coder", memory_dir=memdir)
    assert chain == ["ollama"]               # local Qwen fine, CLI excluded
    from core.routing import read_decisions
    reasons = [r["reason"] for r in
               read_decisions(memdir)[-1]["rejected"]]
    assert any("unverified" in r for r in reasons)
    # a passing smoke test admits it
    record_verification(memdir, "qwen", True)
    chain2 = capability_chain(cfg, "coder", memory_dir=memdir)
    assert set(chain2) == {"qwen", "ollama"}


# -- aggregate report -----------------------------------------------------------------

def test_backend_auth_report_covers_all_types(tmp_path, monkeypatch):
    import core.authx as authx
    monkeypatch.setattr(authx, "claude_auth_detail",
                        lambda *a, **k: authx._report(
                            "claude", "authenticated",
                            method="subscription",
                            autonomous_ready=True))
    monkeypatch.setattr(authx, "qwen_auth_detail",
                        lambda *a, **k: authx._report("qwen",
                                                      "unverified"))
    monkeypatch.setattr(authx, "ollama_auth_detail",
                        lambda *a, **k: authx._report(
                            "ollama", "local_ok", autonomous_ready=True))
    cfg = {"backends": {
        "claude": {"type": "cli", "kind": "configured",
                   "binary": "claude"},
        "qwen": {"type": "cli", "kind": "configured", "binary": "qwen"},
        "ollama": {"type": "local", "model": "qwen3.5"},
        "openai_api": {"type": "api", "provider": "openai"}},
        "providers": {"openai": {"type": "openai",
                                 "api_key_env": "OPENAI_API_KEY"}}}
    memdir = str(tmp_path / "mem")
    record_verification(memdir, "claude", True)
    secret = "sk-" + "z" * 30
    reports = backend_auth_report(cfg, memdir,
                                  env={"OPENAI_API_KEY": secret})
    assert reports["claude"]["state"] == "authenticated"
    assert reports["claude"]["smoke_test"]["ok"] is True
    assert reports["qwen"]["state"] == "unverified"
    assert reports["ollama"]["state"] == "local_ok"
    assert reports["openai_api"]["state"] == "authenticated"
    assert "OPENAI_API_KEY" in reports["openai_api"]["method"]
    assert secret not in json.dumps(reports)   # never the value


def test_verification_roundtrip(tmp_path):
    memdir = str(tmp_path)
    assert read_verification(memdir) == {}
    record_verification(memdir, "codex", True, "ok")
    record_verification(memdir, "qwen", False, "refused")
    data = read_verification(memdir)
    assert data["codex"]["ok"] is True
    assert data["qwen"]["ok"] is False
