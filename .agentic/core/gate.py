"""Deterministic verification gate — the final technical vote. No model can
override it. Checks come from config (verification.commands) or are
auto-detected from the repository. A pre-existing baseline failure is not a
regression, but it is reported honestly; a NEW failure always fails the gate.
"""
import datetime as _dt
import json
import os
import shlex
import functools
import http.server
import posixpath
import re
import threading
import urllib.parse
import urllib.request

from . import execpolicy

# Common Unix utilities a task/architect-authored check might name,
# never guaranteed present on a native Windows host (item 7 of the
# canonical-contract-divergence fix): auto-detection (`detect_commands`
# above) never generates these, but an admin/architect-authored
# `verification.commands` entry might.
_UNIX_ONLY_BASE_COMMANDS = ("ls", "cat", "grep", "test", "find", "touch")


def _is_unix_only_command(command):
    try:
        tokens = shlex.split(str(command), posix=True)
    except ValueError:
        return False
    return bool(tokens) and tokens[0] in _UNIX_ONLY_BASE_COMMANDS


def _platform_neutral_existence_check(command, repo_root):
    """Translates the common POSIX file/directory-existence idiom
    (`test -f/-d PATH`, or `[ -f/-d PATH ]`) into a safe, internal Python
    check -- no shell, no external tool, identical behaviour on every
    platform (item 7: "structural checks must use the safe capability
    layer, not shell syntax"). Returns (ok, detail), or `None` when
    `command` isn't one of these two recognised idioms (never guessed)."""
    try:
        tokens = shlex.split(str(command), posix=True)
    except ValueError:
        return None
    if len(tokens) == 3 and tokens[0] == "test" and tokens[1] in ("-f", "-d"):
        flag, path = tokens[1], tokens[2]
    elif len(tokens) == 4 and tokens[0] == "[" and tokens[-1] == "]" and \
            tokens[1] in ("-f", "-d"):
        flag, path = tokens[1], tokens[2]
    else:
        return None
    full = os.path.join(repo_root, path)
    exists = os.path.isfile(full) if flag == "-f" else os.path.isdir(full)
    kind = "file" if flag == "-f" else "directory"
    return exists, "%s %r %s" % (kind, path,
                                "exists" if exists else "does not exist")


def _platform_neutral_read_check(command, repo_root):
    """Translate exactly `cat RELATIVE_FILE` into a bounded internal read.

    This preserves the read-only intent of a common documentation check on
    every OS without invoking a shell. Options, absolute paths, traversal,
    multiple operands, directories, and unreadable files are rejected.
    """
    try:
        tokens = shlex.split(str(command), posix=True)
    except ValueError:
        return None
    if len(tokens) != 2 or tokens[0] != "cat":
        return None
    path = tokens[1]
    if not path or path.startswith("-") or os.path.isabs(path):
        return None
    root = os.path.realpath(repo_root)
    full = os.path.realpath(os.path.join(root, path))
    try:
        if os.path.commonpath([root, full]) != root:
            return None
    except ValueError:
        return None
    if not os.path.isfile(full):
        return False, "file %r does not exist" % path
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        return False, "file %r is not readable: %s" % (path, exc)
    return True, content[-1200:]


_LOCAL_ASSET_RE = re.compile(
    String.raw\`(?i)(?:src|href)\s*=\s*["']([^"'#?]+)["']|\`
    String.raw\`["']([^"']+\.(?:html|css|js|mjs))["']\`)


def _safe_local_asset(base_path, reference):
    """Resolve a browser asset reference without permitting external access
    or traversal outside the project root."""
    parsed = urllib.parse.urlsplit(str(reference))
    if parsed.scheme or parsed.netloc or str(reference).startswith("//"):
        return None
    raw_path = urllib.parse.unquote(parsed.path).replace("\\", "/")
    if raw_path.startswith("/"):
        candidate = posixpath.normpath(raw_path.lstrip("/"))
    else:
        candidate = posixpath.normpath(posixpath.join(
            posixpath.dirname(base_path), raw_path))
    if candidate in ("", ".") or candidate == ".." or candidate.startswith("../"):
        return None
    return candidate


def run_local_static_app_smoke(repo_root):
    """Serve a static application on loopback and verify its load graph.

    This is deterministic evidence for source-plan criteria such as
    "the application opens locally in a browser". It deliberately does not
    contact an external network or invoke a shell. The check loads index.html,
    verifies the application mount, follows local HTML script/style references
    and local HTML/CSS/JS references found in those assets, and requires every
    discovered resource to return HTTP 200.
    """
    index_path = os.path.join(repo_root, "index.html")
    record = {
        "name": "local-static-app-smoke",
        "command": "internal: serve and load static app on loopback",
        "mandatory": True,
        "passed": False,
        "exit_code": 1,
        "detail": "",
        "kind": "structural",
        "platform_neutral": True,
    }
    if not os.path.isfile(index_path):
        record["detail"] = "index.html does not exist"
        return record

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            pass

    handler = functools.partial(QuietHandler, directory=repo_root)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    loaded = []
    try:
        thread.start()
        origin = "http://127.0.0.1:%d/" % server.server_address[1]
        pending = ["index.html"]
        seen = set()
        while pending:
            asset = pending.pop(0)
            if asset in seen:
                continue
            seen.add(asset)
            with urllib.request.urlopen(
                    urllib.parse.urljoin(origin, asset), timeout=5) as response:
                if response.status != 200:
                    raise OSError("%s returned HTTP %s" %
                                  (asset, response.status))
                body = response.read(1024 * 1024)
            loaded.append(asset)
            if asset == "index.html" and not re.search(
                    rb'id\s*=\s*["\x27]app["\x27]', body, re.I):
                raise ValueError("index.html has no #app mount")
            if asset.endswith((".html", ".js", ".mjs", ".css")):
                text_body = body.decode("utf-8", errors="replace")
                for match in _LOCAL_ASSET_RE.finditer(text_body):
                    reference = match.group(1) or match.group(2)
                    resolved = _safe_local_asset(asset, reference)
                    if resolved and resolved not in seen:
                        pending.append(resolved)
        record["passed"] = True
        record["exit_code"] = 0
        record["detail"] = (
            "served and loaded static app over loopback HTTP; #app mount "
            "present; loaded local assets: %s" % ", ".join(loaded))
    except Exception as exc:  # noqa: BLE001
        record["detail"] = "local static app load failed: %s" % exc
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return record


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


def run_checks(cfg, workdir, log_dir=None, timeout=None,
               required_commands=None):
    """Run every configured check in workdir. Mandatory checks are never
    skipped; a missing log_dir only skips log persistence, not checks.

    ZERO configured/detected checks is a BLOCKING failure (`ok: false`,
    `no_checks: true`): a repository without deterministic verification can
    never pass the gate, and no AI verdict may convert that into success."""
    commands, auto = resolve_commands(cfg, workdir)
    commands = list(commands)
    existing = {str(c.get("command")) for c in commands}
    # Explicit administrator verification commands are authoritative (and
    # are used by network-free acceptance fixtures as safe substitutes).
    # Canonical task checks augment auto-detection, or can be opted into
    # alongside explicit checks with include_task_checks: true.
    include_task_checks = auto or bool(
        (cfg.get("verification") or {}).get("include_task_checks", False))
    for index, raw in enumerate(
            (required_commands or []) if include_task_checks else [], 1):
        check = dict(raw) if isinstance(raw, dict) else {
            "name": "task-deterministic-%d" % index,
            "command": str(raw),
            "mandatory": True,
            "kind": "test_suite",
        }
        command = str(check.get("command") or "")
        if not command or command in existing:
            continue
        # A task contract may express portable fallback semantics as
        # "first || second". Preserve that meaning without ever enabling a
        # shell: each alternative is independently parsed and executed by
        # execpolicy with shell=False.
        alternatives = [part.strip() for part in command.split(" || ")
                        if part.strip()]
        if len(alternatives) > 1:
            check["alternatives"] = alternatives
        commands.append(check)
        existing.add(command)
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
        # item 7: a recognised POSIX existence idiom never shells out --
        # the safe capability layer's own primitive (a plain os.path
        # check) is both correct and platform-neutral, on every OS.
        neutral = _platform_neutral_existence_check(check["command"], workdir)
        if neutral is None:
            neutral = _platform_neutral_read_check(check["command"], workdir)
        if neutral is not None:
            record["passed"], record["detail"] = neutral
            record["exit_code"] = 0 if record["passed"] else 1
            record["platform_neutral"] = True
        elif os.name == "nt" and _is_unix_only_command(check["command"]):
            # an unrecognised Unix-only utility on native Windows: never
            # silently execute it (it may not exist, or behave
            # differently than intended) and never silently report it as
            # passed either -- skipped, clearly labelled, still counts
            # against a mandatory check exactly like any other failure.
            record["passed"] = False
            record["exit_code"] = None
            record["detail"] = ("skipped: %r is a Unix-only command with "
                                "no platform-neutral equivalent on this "
                                "OS; replace it with an internal "
                                "deterministic check" % check["command"])
            record["platform_neutral"] = False
            record["skipped_unix_only"] = True
        else:
            alternatives = check.get("alternatives") or [check["command"]]
            attempts = []
            run = None
            for alternative in alternatives:
                run = execpolicy.run_command(
                    alternative, cwd=workdir, timeout=timeout,
                    shell_required=bool(check.get("shell_required", False)),
                    source="config")
                attempts.append(run)
                if run["exit_code"] == 0 and not run["timed_out"]:
                    break
            record["exit_code"] = run["exit_code"]
            record["passed"] = run["exit_code"] == 0 and not run["timed_out"]
            output = "\n".join(
                (attempt["stdout"] + attempt["stderr"]).strip()
                for attempt in attempts)
            record["detail"] = ("timed out after %ss" % timeout
                                if run["timed_out"]
                                else output[-400:].strip())
            if len(attempts) > 1:
                record["attempted_alternatives"] = [
                    attempt["argv"] for attempt in attempts]
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
                with open(os.path.join(log_dir, name + ".log"), "w",
                          encoding="utf-8", errors="replace") as fh:
                    for attempt in attempts:
                        attempt_output = attempt["stdout"] + attempt["stderr"]
                        fh.write("$ %s\nexit: %s\n\n%s\n"
                                 % (attempt["argv"], attempt["exit_code"],
                                    attempt_output))
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
