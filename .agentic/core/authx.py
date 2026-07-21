"""Accurate CLI authentication detection (MP Phase 8).

Fixes the blanket "auth unknown": Claude uses the supported
`claude auth status` command (JSON first, text fallback, exit code);
Qwen is reported honestly (the old standalone `qwen auth` no longer
exists, the OAuth free tier is discontinued, configuration alone is NOT
authentication); Ollama-hosted Qwen-family models are a separate backend
with local authentication. Credential VALUES are never read or printed —
only the presence of well-known environment variable NAMES is reported.

States: authenticated | not_authenticated | expired |
conflicting_credentials | executable_missing | probe_failed | unverified |
local_ok | unknown

Verification results (opt-in smoke tests, user-invoked) persist in
<memory>/auth-verification.json; an UNVERIFIED Qwen CLI is excluded from
autonomous routing until a smoke test passes.
"""
import json
import os
import shutil

from providers.cli_base import validate_cli_command
from core import execpolicy

AUTH_STATES = ("authenticated", "not_authenticated", "expired",
               "conflicting_credentials", "executable_missing",
               "probe_failed", "unverified", "local_ok", "unknown")

VERIFICATION_FILE = "auth-verification.json"


def _default_runner(argv, cwd=None, timeout=30, stdin_text=None):
    return execpolicy.run_command(argv, cwd=cwd or ".", timeout=timeout,
                                  source="config", stdin_text=stdin_text)


def _report(backend, state, method=None, detail=None, instructions=None,
            conflict=None, autonomous_ready=False):
    return {"backend": backend, "state": state, "method": method,
            "detail": (detail or "")[:300],
            "instructions": instructions,
            "credential_conflict": conflict,
            "autonomous_ready": bool(autonomous_ready)}


# -- Claude -----------------------------------------------------------------------

def claude_auth_detail(binary="claude", runner=None, which=None, env=None):
    runner = runner or _default_runner
    which = which or shutil.which
    env = env if env is not None else os.environ
    if not which(binary):
        return _report("claude", "executable_missing",
                       instructions="install Claude Code, then run "
                                    "`claude auth login`")
    run = runner(validate_cli_command([binary, "auth", "status"]),
                 timeout=30)
    output = (run.get("stdout") or "") + "\n" + (run.get("stderr") or "")
    low = output.lower()
    if run.get("timed_out"):
        return _report("claude", "probe_failed", detail="status probe "
                       "timed out")
    if run.get("exit_code") == 127 or "unknown command" in low or \
            "not recognized" in low or "no such command" in low:
        # older CLI without `auth status`: honest, with guidance
        return _report("claude", "probe_failed",
                       detail="this Claude Code version has no supported "
                              "`auth status`; update the CLI",
                       instructions="update Claude Code, then "
                                    "`claude auth status`")

    state, method = _parse_claude_status(output, run.get("exit_code"))
    conflict = None
    if state == "authenticated" and (method or "").lower() not in \
            ("api_key", "apikey", "api key") and env.get("ANTHROPIC_API_KEY"):
        conflict = ("ANTHROPIC_API_KEY is set while subscription auth is "
                    "active; the API key may take precedence and bill "
                    "separately — unset it if you want subscription usage")
        state = "conflicting_credentials"
    ready = state in ("authenticated",)
    instructions = None if ready else \
        "run `claude auth login` (interactive) to authenticate"
    return _report("claude", state, method=method,
                   detail=output.strip()[:200], instructions=instructions,
                   conflict=conflict,
                   autonomous_ready=ready or
                   state == "conflicting_credentials")


def _parse_claude_status(output, exit_code):
    """JSON first; text fallback; exit code as the tie-breaker."""
    from core.jsonx import extract_first_json
    data = extract_first_json(output)
    if isinstance(data, dict):
        logged = data.get("loggedIn", data.get("logged_in",
                          data.get("authenticated")))
        method = data.get("authMethod") or data.get("auth_method") \
            or data.get("method") or data.get("subscriptionType")
        if logged is True:
            return "authenticated", method
        if logged is False:
            return "not_authenticated", method
        if str(data.get("status", "")).lower() in ("expired",):
            return "expired", method
    low = output.lower()
    if "expired" in low:
        return "expired", None
    if "not logged in" in low or "please log in" in low or \
            "login required" in low:
        return "not_authenticated", None
    if "logged in" in low or "authenticated" in low:
        method = None
        if "claude.ai" in low or "subscription" in low or "oauth" in low:
            method = "subscription"
        elif "api key" in low or "api_key" in low:
            method = "api_key"
        return "authenticated", method
    if exit_code == 0:
        return "authenticated", None
    if exit_code and exit_code != 0:
        return "not_authenticated", None
    return "unknown", None


# -- Qwen --------------------------------------------------------------------------

def qwen_auth_detail(binary="qwen", runner=None, which=None,
                     memory_dir=None, home=None):
    """Honest Qwen CLI reporting: configuration alone is never claimed as
    authentication; readiness requires an opt-in smoke test."""
    runner = runner or _default_runner
    which = which or shutil.which
    home = home or os.path.expanduser("~")
    if not which(binary):
        return _report(
            "qwen", "executable_missing",
            detail="Qwen Code CLI not installed",
            instructions="install Qwen Code if you want the CLI; local "
                         "Qwen models via Ollama do NOT need it")
    run = runner(validate_cli_command([binary, "--version"]), timeout=30)
    version = ((run.get("stdout") or "") + (run.get("stderr") or "")) \
        .strip().splitlines()
    version = version[0][:60] if version else None
    config_present = any(os.path.exists(os.path.join(home, p)) for p in
                         (".qwen/settings.json", ".qwen/config.json",
                          ".qwen/oauth_creds.json"))
    verified = read_verification(memory_dir).get("qwen", {}) \
        if memory_dir else {}
    if verified.get("ok"):
        return _report("qwen", "authenticated", method="verified by "
                       "smoke test", detail="version %s" % version,
                       autonomous_ready=True)
    detail = ("version %s; %s. The standalone `qwen auth` command and the "
              "OAuth free tier are discontinued — configuration presence "
              "is NOT authentication."
              % (version,
                 "configuration found" if config_present
                 else "no configuration found"))
    return _report(
        "qwen", "unverified", detail=detail,
        instructions="launch `qwen` interactively, use /auth to set up "
                     "credentials and /doctor to verify; then run "
                     "`agentic backends smoke qwen` (opt-in, consumes "
                     "quota) to enable autonomous routing",
        autonomous_ready=False)


# -- Ollama (Qwen-family local models are NOT the Qwen CLI) -------------------------

def ollama_auth_detail(runner=None, which=None):
    from providers.local_ollama import detect_ollama
    info = detect_ollama(runner=runner, which=which)
    if not info["installed"]:
        return _report("ollama", "executable_missing",
                       instructions="install Ollama from ollama.com")
    return _report("ollama", "local_ok", method="local runtime — no "
                   "authentication", detail="models: %s"
                   % ", ".join(info.get("models", [])[:6]),
                   autonomous_ready=bool(info.get("models")))


# -- verification persistence (opt-in smoke results) --------------------------------

def verification_path(memory_dir):
    return os.path.join(memory_dir, VERIFICATION_FILE)


def read_verification(memory_dir):
    path = verification_path(memory_dir)
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return {}

def _sanitize_detail(detail):
    """Plain string details are kept as-is; structured diagnostics (e.g.
    Codex's smoke-test verdict: reason/event_types/exit_code/argv) are kept
    as a bounded dict so doctor can render them. Never carries credential
    material -- callers are responsible for redacting the prompt/argv
    before this point (see `_redact_argv` in cli_codex.py)."""
    if isinstance(detail, dict):
        out = {"reason": str(detail.get("reason", ""))[:200]}
        if detail.get("event_types"):
            out["event_types"] = [str(t) for t in detail["event_types"]][:12]
        if "exit_code" in detail:
            out["exit_code"] = detail["exit_code"]
        if "timed_out" in detail:
            out["timed_out"] = bool(detail["timed_out"])
        if detail.get("argv"):
            out["argv"] = [str(a) for a in detail["argv"]][:20]
        if detail.get("cwd"):
            out["cwd"] = str(detail["cwd"])[:300]
        return out
    return str(detail)[:200]


def record_verification(memory_dir, backend, ok, detail=""):
    import datetime as _dt
    data = read_verification(memory_dir)
    data[backend] = {"ok": bool(ok), "detail": _sanitize_detail(detail),
                     "at": _dt.datetime.now().isoformat(
                         timespec="seconds")}
    os.makedirs(memory_dir, exist_ok=True)
    tmp = verification_path(memory_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, verification_path(memory_dir))
    if ok:
        # a fresh, successful smoke/auth verification is real evidence the
        # backend works right now -- let a stale, locally-inferred circuit
        # breaker recover early rather than staying disabled on a timer
        # (never overrides a provider-stated rate/usage limit; see
        # BreakerBoard.recover_if_verified).
        from .breaker import BreakerBoard
        BreakerBoard(memory_dir).recover_if_verified(backend)
    return data[backend]


# -- aggregate report ----------------------------------------------------------------

def backend_auth_report(cfg, memory_dir, runner=None, which=None,
                        env=None):
    """Per-backend authentication detail for doctor/dashboard/routing.
    Codex keeps its existing (working) probe; API backends report key
    NAME presence only."""
    env = env if env is not None else os.environ
    reports = {}
    for name, bcfg in (cfg.get("backends") or {}).items():
        btype = (bcfg or {}).get("type", "api")
        kind = (bcfg or {}).get("kind", name)
        if btype == "cli" and (kind == "configured" and
                               (bcfg.get("binary") == "claude"
                                or name == "claude")):
            reports[name] = claude_auth_detail(bcfg.get("binary", "claude"),
                                               runner=runner, which=which,
                                               env=env)
        elif btype == "cli" and (name == "qwen" or
                                 bcfg.get("binary") == "qwen"):
            reports[name] = qwen_auth_detail(bcfg.get("binary", "qwen"),
                                             runner=runner, which=which,
                                             memory_dir=memory_dir)
        elif btype == "cli":
            from core.backends import build_backend
            try:
                adapter = build_backend(cfg, name, runner=runner,
                                        which=which)
                status = adapter.auth_status()
                installed = adapter.detect().get("installed", False)
            except Exception as exc:   # noqa: BLE001
                reports[name] = _report(name, "probe_failed",
                                        detail=str(exc)[:200])
                continue
            if not installed:
                reports[name] = _report(name, "executable_missing")
            else:
                state = {"ok": "authenticated",
                         "required": "not_authenticated"}.get(status,
                                                              "unknown")
                # login status alone is not enough: a recorded smoke-test
                # FAILURE (e.g. a malformed invocation) must downgrade
                # readiness even though the CLI is authenticated -- login
                # succeeding was never proof the invocation itself worked.
                verified = read_verification(memory_dir).get(name) \
                    if memory_dir else None
                ready = state == "authenticated" and \
                    (verified is None or verified.get("ok") is not False)
                reports[name] = _report(name, state, autonomous_ready=ready)
        elif btype == "local":
            reports[name] = ollama_auth_detail(runner=runner, which=which)
        else:
            provider = (bcfg or {}).get("provider", name)
            pcfg = (cfg.get("providers") or {}).get(provider) or {}
            key_env = pcfg.get("api_key_env")
            present = bool(env.get(key_env)) if key_env else \
                not pcfg.get("api_key_required", True)
            reports[name] = _report(
                name, "authenticated" if present else "not_authenticated",
                method="api key (%s)" % (key_env or "none required"),
                autonomous_ready=present)
        verification = read_verification(memory_dir).get(name) \
            if memory_dir else None
        if verification is not None:
            reports[name]["smoke_test"] = verification
    return reports
