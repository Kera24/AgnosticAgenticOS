"""Bootstrap-eligibility and structural deterministic checks.

Fixes the bootstrap deadlock: a brand-new project's very first
scaffolding task has no test framework yet, so `gate.run_checks` always
reports `no_checks: True` and the cycle blocks forever (`zero
deterministic checks: blocking`) -- even though the task itself produced
real, verifiable output.

The invariant this module must never violate: "no checks configured" can
never be reported as "tests passed". A bootstrap task that clears its
structural gate gets `tests: not_configured_yet`, never `tests: passed`
-- only an actual test_suite check (see `gate.py`) can ever produce
`tests: passed`. AI/model output is never consulted here; every check in
this module is a deterministic, code-level assertion over the worktree.
"""
import json
import os
import shutil
import subprocess

from . import gitops

BOOTSTRAP_KIND = "bootstrap"
TEST_SETUP_KINDS = ("test_setup", "testing_setup", "test_framework")

NO_CHECKS_BLOCKER_REASON = "no deterministic checks configured"
NO_CHECKS_HUMAN_REASON = (NO_CHECKS_BLOCKER_REASON +
                          "; configure verification.commands")

CREDENTIAL_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_rsa.*",
    "*credential*", "*credentials*", "*secret*", "*secrets*",
    ".npmrc", ".netrc",
]
MANIFEST_FILES = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod")


# -- classification -----------------------------------------------------------

def is_bootstrap_task(task):
    return str((task or {}).get("kind") or "").strip().lower() == \
        BOOTSTRAP_KIND


def is_test_setup_task(task):
    return str((task or {}).get("kind") or "").strip().lower() in \
        TEST_SETUP_KINDS


def test_framework_scheduled(backlog):
    """True once the architecture has committed to a task that installs
    the test framework -- anywhere in the backlog, regardless of status:
    that is what "the architecture confirms a test framework is
    scheduled" means. Business-logic tasks never get this exception
    merely because such a task doesn't exist yet."""
    return any(is_test_setup_task(t) for t in backlog or [])


def bootstrap_eligible(task, backlog):
    """Returns (eligible, reason). Only a task explicitly classified as
    bootstrap/scaffolding, in a project whose own backlog already commits
    to a later test-setup task, may substitute structural checks for a
    test suite. Every other zero-check task blocks exactly as before."""
    if not is_bootstrap_task(task):
        return False, "task is not classified as bootstrap/scaffolding"
    if not test_framework_scheduled(backlog):
        return False, ("no test-framework task scheduled in the backlog; "
                       "add one (kind: test_setup) before using the "
                       "bootstrap exception")
    return True, None


# -- structural checks ---------------------------------------------------------

def _result(name, kind, mandatory, passed, detail, applicable=True):
    return {"name": name, "command": "(structural check: %s)" % name,
            "mandatory": mandatory, "passed": passed,
            "exit_code": 0 if passed else 1, "detail": detail, "kind": kind,
            "applicable": applicable}


def _check_files_changed(changed):
    passed = bool(changed)
    return _result("bootstrap-files-created", "structural", True, passed,
                  "no files were created or modified" if not passed
                  else "%d file(s) changed" % len(changed))


def _check_files_non_empty(worktree, changed_status):
    empty = []
    for path, status in changed_status:
        if status.startswith("D"):
            continue
        full = os.path.join(worktree, path)
        try:
            if not os.path.exists(full) or os.path.getsize(full) == 0:
                empty.append(path)
        except OSError:
            empty.append(path)
    passed = not empty
    return _result("bootstrap-files-non-empty", "structural", True, passed,
                  "empty file(s): %s" % ", ".join(empty) if empty
                  else "all changed files are non-empty")


def _check_root_containment(worktree, changed):
    escaped = []
    for path in changed:
        try:
            gitops.safe_join(worktree, path)
        except Exception:   # noqa: BLE001 -- any rejection means it escapes
            escaped.append(path)
    passed = not escaped
    return _result("bootstrap-project-root-containment", "project_isolation",
                   True, passed,
                   "path(s) escape the project root: %s" % ", ".join(escaped)
                   if escaped
                   else "all changed files stay inside the project root")


def _check_expected_paths(task, changed):
    expected = (task or {}).get("expected_paths") or []
    if not expected:
        return _result("bootstrap-expected-paths", "project_isolation", True,
                       True, "task declares no expected_paths to check",
                       applicable=False)
    outside = [p for p in changed if not gitops.matches_any(p, expected)]
    passed = not outside
    return _result("bootstrap-expected-paths", "project_isolation", True,
                   passed,
                   "file(s) outside task's expected_paths: %s"
                   % ", ".join(outside) if outside
                   else "all changed files are within expected_paths")


def _check_git_valid(worktree):
    ok = gitops.is_repo(worktree)
    return _result("bootstrap-git-valid", "structural", True, ok,
                   "git worktree is valid" if ok
                   else "git worktree is not a valid repository")


def _check_no_credentials(changed):
    hits = [p for p in changed if gitops.matches_any(p, CREDENTIAL_PATTERNS)]
    passed = not hits
    return _result("bootstrap-no-credential-files", "security", True, passed,
                   "prohibited credential-like file(s): %s" % ", ".join(hits)
                   if hits else "no credential-like files present")


def _check_manifest_parses(worktree):
    present = [m for m in MANIFEST_FILES
              if os.path.exists(os.path.join(worktree, m))]
    if not present:
        return _result("bootstrap-manifest-parses", "structural", True, True,
                       "no manifest file present", applicable=False)
    errors = []
    for name in present:
        full = os.path.join(worktree, name)
        try:
            with open(full, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            errors.append("%s: %s" % (name, exc))
            continue
        if name == "package.json":
            try:
                json.loads(text)
            except ValueError as exc:
                errors.append("%s: %s" % (name, exc))
        # pyproject.toml/Cargo.toml/go.mod: best-effort presence + non-empty
        # is already covered by the non-empty check; a strict TOML parse is
        # only attempted when a parser is available (3.11+ stdlib tomllib).
        elif name in ("pyproject.toml", "Cargo.toml"):
            try:
                import tomllib
            except ModuleNotFoundError:
                continue
            try:
                tomllib.loads(text)
            except Exception as exc:   # noqa: BLE001
                errors.append("%s: %s" % (name, exc))
    passed = not errors
    return _result("bootstrap-manifest-parses", "structural", True, passed,
                   "; ".join(errors) if errors
                   else "manifest(s) parse: %s" % ", ".join(present))


def _check_entry_points_exist(worktree):
    package_json = os.path.join(worktree, "package.json")
    if not os.path.exists(package_json):
        return _result("bootstrap-entry-points-exist", "structural", True,
                       True, "no package.json present", applicable=False)
    try:
        with open(package_json, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _result("bootstrap-entry-points-exist", "structural", True,
                       True, "package.json does not parse; covered by "
                       "bootstrap-manifest-parses", applicable=False)
    main = data.get("main")
    if not main:
        return _result("bootstrap-entry-points-exist", "structural", True,
                       True, "package.json declares no main entry point",
                       applicable=False)
    exists = os.path.exists(os.path.join(worktree, main))
    return _result("bootstrap-entry-points-exist", "structural", True,
                   exists,
                   "package.json main entry point exists: %s" % main
                   if exists
                   else "package.json main entry point missing: %s" % main)


def _check_html_structural(worktree, changed):
    html_files = [p for p in changed if p.lower().endswith((".html", ".htm"))
                 and os.path.exists(os.path.join(worktree, p))]
    if not html_files:
        return _result("bootstrap-html-parses", "structural", True, True,
                       "no HTML files changed", applicable=False)
    from html.parser import HTMLParser

    class _TagSeen(HTMLParser):
        def __init__(self):
            super().__init__()
            self.saw_tag = False

        def handle_starttag(self, tag, attrs):
            self.saw_tag = True

    bad = []
    for path in html_files:
        full = os.path.join(worktree, path)
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            parser = _TagSeen()
            parser.feed(text)
            parser.close()
            if not parser.saw_tag:
                bad.append("%s: no structural elements" % path)
        except Exception as exc:   # noqa: BLE001
            bad.append("%s: %s" % (path, exc))
    passed = not bad
    return _result("bootstrap-html-parses", "structural", True, passed,
                   "; ".join(bad) if bad
                   else "HTML file(s) parse: %s" % ", ".join(html_files))


def _check_js_syntax(worktree, changed):
    checkable = [p for p in changed if p.lower().endswith((".js", ".jsx"))
                and os.path.exists(os.path.join(worktree, p))]
    if not checkable:
        return _result("bootstrap-js-syntax", "syntax", True, True,
                       "no plain JavaScript files changed",
                       applicable=False)
    node = shutil.which("node")
    if not node:
        return _result("bootstrap-js-syntax", "syntax", True, True,
                       "node not available; syntax check skipped",
                       applicable=False)
    bad = []
    for path in checkable:
        full = os.path.join(worktree, path)
        proc = subprocess.run([node, "--check", full], capture_output=True,
                              text=True)
        if proc.returncode != 0:
            bad.append("%s: %s" % (path, proc.stderr.strip()[:200]))
    passed = not bad
    return _result("bootstrap-js-syntax", "syntax", True, passed,
                   "; ".join(bad) if bad
                   else "JavaScript file(s) pass syntax check: %s"
                   % ", ".join(checkable))


def run_structural_checks(task, worktree, log_dir=None):
    """The bootstrap gate: deterministic, code-only checks derived from the
    task's declared outputs. Returns the same shape as `gate.run_checks`
    plus `tests` (always "not_configured_yet" here -- a structural pass is
    reported as a structural pass, never a test pass) and `bootstrap_mode`.
    """
    changed = gitops.changed_files(worktree)
    changed_status = gitops.changed_files_with_status(worktree)
    checks = [
        _check_files_changed(changed),
        _check_files_non_empty(worktree, changed_status),
        _check_root_containment(worktree, changed),
        _check_expected_paths(task, changed),
        _check_git_valid(worktree),
        _check_no_credentials(changed),
        _check_manifest_parses(worktree),
        _check_entry_points_exist(worktree),
        _check_html_structural(worktree, changed),
        _check_js_syntax(worktree, changed),
    ]
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        for c in checks:
            with open(os.path.join(log_dir, c["name"] + ".log"), "w",
                      encoding="utf-8", errors="replace") as fh:
                fh.write("kind: %s\napplicable: %s\npassed: %s\n\n%s"
                         % (c["kind"], c["applicable"], c["passed"],
                            c["detail"]))
    applicable = [c for c in checks if c["applicable"]]
    ok = bool(applicable) and all(c["passed"] for c in applicable
                                  if c["mandatory"])
    return {"ok": ok, "results": checks, "no_checks": not applicable,
            "auto_detected": True, "tests": "not_configured_yet",
            "bootstrap_mode": True}


# -- recovery -------------------------------------------------------------------

def recover_bootstrap_deadlock(agentic_dir):
    """Self-healing for tasks blocked solely by the pre-fix zero-check
    deadlock: if a blocked task is bootstrap-eligible under the new rules,
    make it retryable again. Never marks it done, never touches worktrees
    -- it only clears the stale block so the next cycle re-attempts the
    task under the fixed gate."""
    from . import projstate
    if not projstate.exists(agentic_dir):
        return []
    backlog = projstate.load_backlog(agentic_dir)
    recovered = [t["id"] for t in backlog
                if t["status"] == "blocked"
                and NO_CHECKS_BLOCKER_REASON in (t.get("blocking_reason")
                                                 or "")
                and bootstrap_eligible(t, backlog)[0]]
    for task_id in recovered:
        projstate.update_task(agentic_dir, task_id, status="pending",
                              blocking_reason=None)
    if recovered:
        blockers = projstate.read_yaml(agentic_dir, "blockers.yaml",
                                       {"blockers": []})
        changed = False
        for b in blockers.get("blockers", []):
            if b.get("task") in recovered and not b.get("resolved") and \
                    NO_CHECKS_BLOCKER_REASON in (b.get("reason") or ""):
                b["resolved"] = True
                changed = True
        if changed:
            projstate.write_yaml(agentic_dir, "blockers.yaml", blockers)
    return recovered
