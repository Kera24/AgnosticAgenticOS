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
import re
import shutil
import subprocess

from . import errors, gitops, projstate

_GLOB_CHARS_RE = re.compile(r"[*?\[]")

BOOTSTRAP_KIND = "bootstrap"
TEST_SETUP_KINDS = ("test_setup", "testing_setup", "test_framework")

NO_CHECKS_BLOCKER_REASON = "no deterministic checks configured"
NO_CHECKS_HUMAN_REASON = (NO_CHECKS_BLOCKER_REASON +
                          "; configure verification.commands")
DETERMINISTIC_CHECKS_MISSING_CODE = \
    projstate.BLOCKER_CODE_DETERMINISTIC_CHECKS_MISSING

# Matches every historical wording/punctuation variant of the pre-fix
# zero-check deadlock message, on either a task's `blocking_reason` or a
# blocker's `reason` -- "no deterministic checks configured", "no
# deterministic checks configured; configure verification.commands",
# "zero deterministic checks: blocking", etc.
_LEGACY_DETERMINISTIC_CHECKS_RE = re.compile(
    r"(no|zero)\s+deterministic\s+checks?\b", re.I)

_TEST_FRAMEWORK_DECISION_RE = re.compile(r"test(ing)?\s*framework", re.I)

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


def decisions_text(agentic_dir):
    """Flattened text of every recorded architecture decision (still
    needed AND already decided) -- used as evidence that "the
    architecture confirms a test framework is scheduled" for projects
    generated before task `kind` existed (see `test_framework_scheduled`
    and `recover_bootstrap_deadlock`)."""
    doc = projstate.read_yaml(agentic_dir, "decisions.yaml",
                              {"human_decisions_needed": [], "decided": []})
    parts = list(doc.get("human_decisions_needed") or [])
    parts += [d.get("decision", "") for d in (doc.get("decided") or [])]
    return "\n".join(parts)


def test_framework_scheduled(backlog, decisions=""):
    """True once the architecture has committed to installing a test
    framework -- either a backlog task explicitly marked `kind:
    test_setup` (new-style projects), or a recorded decision (needed or
    already resolved) about which test framework to use (older projects,
    where that commitment was only ever captured as a human_decision).
    Business-logic tasks never get this exception merely because neither
    exists yet."""
    if any(is_test_setup_task(t) for t in backlog or []):
        return True
    return bool(_TEST_FRAMEWORK_DECISION_RE.search(decisions or ""))


def bootstrap_eligible(task, backlog, decisions=""):
    """Returns (eligible, reason). Only a task explicitly classified as
    bootstrap/scaffolding, in a project that has committed to a test
    framework somewhere (see `test_framework_scheduled`), may substitute
    structural checks for a test suite. Every other zero-check task
    blocks exactly as before."""
    if not is_bootstrap_task(task):
        return False, "task is not classified as bootstrap/scaffolding"
    if not test_framework_scheduled(backlog, decisions):
        return False, ("no test-framework commitment on record (backlog "
                       "task kind: test_setup, or a recorded decision); "
                       "add one before using the bootstrap exception")
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


def normalize_expected_entry(entry):
    """Normalise one `task.expected_paths` entry -- the new explicit dict
    form ({"path", "type", "required", "non_empty"}) or a legacy string
    -- into {"path", "type", "required", "non_empty"}. `type` is one of
    "file" / "directory" / "glob" / "unknown". A bare legacy string with
    no trailing "/" and no extension is genuinely ambiguous: per item 4,
    this NEVER guesses file-vs-directory when that guess could affect
    safety -- "unknown" accepts either, as long as SOMETHING exists
    there. A string containing glob metacharacters (*, ?, [) is treated
    as a legacy advisory pattern: "at least one changed file matches",
    never a literal path to stat."""
    if isinstance(entry, dict):
        path = str(entry.get("path", "")).replace("\\", "/").rstrip("/")
        etype = entry.get("type")
        if etype not in ("file", "directory"):
            etype = "directory" if str(entry.get("path", "")).endswith("/") \
                else "unknown"
        return {"path": path, "type": etype,
                "required": bool(entry.get("required", True)),
                "non_empty": bool(entry.get("non_empty", False))}
    text = str(entry).replace("\\", "/")
    if _GLOB_CHARS_RE.search(text):
        return {"path": text, "type": "glob", "required": True,
               "non_empty": False}
    if text.endswith("/"):
        return {"path": text.rstrip("/"), "type": "directory",
               "required": True, "non_empty": False}
    base = os.path.basename(text)
    if "." in base.lstrip("."):
        return {"path": text, "type": "file", "required": True,
               "non_empty": False}
    return {"path": text, "type": "unknown", "required": True,
           "non_empty": False}


def expected_path_strings(task):
    """Flat list of plain path strings from `task.expected_paths`,
    whatever mix of legacy strings and new dict-form entries it contains
    -- for the handful of call sites elsewhere (ownership claims, UI-role
    detection, ...) that only ever needed glob-matchable strings and
    predate the dict form."""
    return [normalize_expected_entry(e)["path"]
           for e in (task or {}).get("expected_paths") or []]


def _check_expected_outputs(task, worktree, changed):
    """The acceptance contract (item 2/3/4/7): every REQUIRED expected
    output must exist and satisfy its declared type -- never a second
    write allowlist. `allowed_paths` (already enforced upstream, before
    the gate even runs) is the only write boundary; this check only ever
    asks "does the required deliverable exist", never "was anything
    unexpected also touched". Distinguishes missing_file /
    missing_directory / wrong_type / empty_required_file so a repair
    attempt gets a precise, actionable reason."""
    raw_entries = (task or {}).get("expected_paths") or []
    if not raw_entries:
        return _result("bootstrap-expected-outputs", "structural", True,
                       True, "task declares no expected_paths to check",
                       applicable=False)
    problems, satisfied = [], []
    for raw in raw_entries:
        entry = normalize_expected_entry(raw)
        path, etype = entry["path"], entry["type"]
        if etype == "glob":
            matched = [p for p in changed if gitops.match_pattern(p, path)]
            if matched:
                satisfied.append("%s (matched %s)" % (path, matched[0]))
            elif entry["required"]:
                problems.append(
                    "missing_file: no changed file matches %r" % path)
            continue
        try:
            full = gitops.safe_join(worktree, path)
        except errors.PolicyError:
            problems.append(
                "invalid_path: expected_paths entry escapes the "
                "worktree: %r" % path)
            continue
        is_file, is_dir = os.path.isfile(full), os.path.isdir(full)
        if etype == "directory":
            if is_file:
                problems.append(
                    "wrong_type: %r expected a directory, found a file"
                    % path)
            elif not is_dir:
                if entry["required"]:
                    problems.append("missing_directory: %r" % path)
            else:
                satisfied.append(path)
        elif etype == "file":
            if is_dir:
                problems.append(
                    "wrong_type: %r expected a file, found a directory"
                    % path)
            elif not is_file:
                if entry["required"]:
                    problems.append("missing_file: %r" % path)
            elif entry["non_empty"] and os.path.getsize(full) == 0:
                problems.append("empty_required_file: %r" % path)
            else:
                satisfied.append(path)
        else:   # unknown -- never guess which type was intended
            if not is_file and not is_dir:
                if entry["required"]:
                    problems.append(
                        "missing_file: %r (type not specified)" % path)
            else:
                satisfied.append(path)
    passed = not problems
    detail = "; ".join(problems) if problems else \
        "all required expected outputs present: %s" % ", ".join(satisfied)
    return _result("bootstrap-expected-outputs", "structural", True, passed,
                   detail)


def _check_additional_files(task, changed):
    """Informational only, never a source of failure: names the changed
    files that are NOT one of the task's required expected outputs
    (item 2 bullet 5 -- "report allowed additional files explicitly in
    evidence"). They are fine precisely because everything reaching this
    point already cleared allowed_paths, the protected-path check, and
    bootstrap-no-credential-files upstream; this is transparency, not a
    second gate."""
    raw_entries = (task or {}).get("expected_paths") or []
    normalized = [normalize_expected_entry(r) for r in raw_entries]
    literal = {e["path"] for e in normalized if e["type"] != "glob"}
    globs = [e["path"] for e in normalized if e["type"] == "glob"]
    extra = [p for p in changed if p not in literal and
            not any(gitops.match_pattern(p, g) for g in globs)]
    return _result("bootstrap-additional-files", "structural", False, True,
                   "additional file(s) beyond required expected outputs "
                   "(allowed -- within allowed_paths and not flagged by "
                   "any other check): %s" % ", ".join(extra) if extra
                   else "no additional files beyond expected outputs",
                   applicable=bool(extra))


_DANGEROUS_GITIGNORE_NEGATION_RE = re.compile(
    r"^!\s*\S*(\.env\b|secret|credential|token|password|session|"
    r"\.pem\b|\.key\b|auth-verification|model-registry)", re.I)


def _check_gitignore_safe(worktree, changed):
    """.gitignore is a normal scaffold-support file -- allowed whenever
    it's within allowed_paths (item 7), never required to also appear in
    expected_paths (that was the exact live bug). The only thing checked
    HERE is that it doesn't contain a negation pattern (`!...`) that
    would un-ignore something matching a credential/runtime pattern.
    Matched on the negated filename itself, not merely on it living
    somewhere under a memory/runtime directory -- a project un-ignoring
    its own known-safe template file (e.g. a memory README) is not
    "dangerous" just because the directory also holds other, genuinely
    sensitive runtime state."""
    gi_path = os.path.join(worktree, ".gitignore")
    if ".gitignore" not in changed or not os.path.exists(gi_path):
        return _result("bootstrap-gitignore-safe", "security", True, True,
                       ".gitignore not changed or not present",
                       applicable=False)
    try:
        with open(gi_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        return _result("bootstrap-gitignore-safe", "security", True, False,
                       "could not read .gitignore: %s" % exc)
    dangerous = [ln.strip() for ln in lines
                if _DANGEROUS_GITIGNORE_NEGATION_RE.match(ln.strip())]
    passed = not dangerous
    return _result("bootstrap-gitignore-safe", "security", True, passed,
                   "dangerous negation pattern(s) in .gitignore expose "
                   "credential/runtime state: %s" % "; ".join(dangerous)
                   if dangerous
                   else ".gitignore contains no dangerous negation "
                        "patterns")


def validate_task_contract(task, allowed_paths):
    """Reject an impossible contract BEFORE the model is ever invoked
    (item 8): every required, non-glob expected output must be creatable
    given `allowed_paths` -- the only write boundary. Returns a list of
    problem strings; empty means the contract is satisfiable with the
    available primitives (file write with auto-created parents, or the
    `mkdir` action for a bare directory)."""
    problems = []
    for raw in (task or {}).get("expected_paths") or []:
        entry = normalize_expected_entry(raw)
        if not entry["required"] or entry["type"] == "glob":
            continue
        path = entry["path"]
        coverable = (gitops.matches_any(path, allowed_paths) or
                    gitops.directory_is_allowed(path, allowed_paths) or
                    gitops.directory_is_allowed(
                        os.path.dirname(path) or ".", allowed_paths))
        if not coverable:
            problems.append(
                "required expected output %r is not coverable by "
                "allowed_paths %r" % (path, allowed_paths))
    return problems


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
        _check_expected_outputs(task, worktree, changed),
        _check_additional_files(task, changed),
        _check_git_valid(worktree),
        _check_no_credentials(changed),
        _check_gitignore_safe(worktree, changed),
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

def _is_legacy_deterministic_checks_block(task):
    return bool(_LEGACY_DETERMINISTIC_CHECKS_RE.search(
        task.get("blocking_reason") or ""))


def _earliest_task_id(agentic_dir, backlog):
    """The task `next_task()` would have picked first, ignoring status --
    i.e. the project's own initial/scaffolding position. Retroactive
    bootstrap classification (see `recover_bootstrap_deadlock`) is only
    ever granted to THIS one task: a business-logic task elsewhere in the
    backlog that also happens to lack `kind` and also hit the same
    deadlock (realistic when verification.commands was project-wide
    empty) must still block -- only the task actually in the scaffolding
    position gets the retroactive exception."""
    if not backlog:
        return None
    order = projstate.milestone_order(agentic_dir)
    rank = {m: i for i, m in enumerate(order)}

    def key(t):
        return (rank.get(t.get("milestone"), len(order)), t["id"])

    return min(backlog, key=key)["id"]


def _resolve_deterministic_checks_blockers(agentic_dir, task_id):
    """Resolve EVERY unresolved blocker recorded for `task_id` that is
    conclusively the zero-check deadlock -- matched by `code` when
    present, and by the legacy reason-text pattern otherwise (covers the
    known duplicate: `fail()` used to write a second, human_only=False
    blocker with the short reason alongside the explicit human_only=True
    one). Backfills `code` on legacy records for future dedup. Never
    touches a blocker for a different task or a different reason."""
    blockers = projstate.read_yaml(agentic_dir, "blockers.yaml",
                                   {"blockers": []})
    resolved = 0
    for b in blockers.get("blockers", []):
        if b.get("resolved") or b.get("task") != task_id:
            continue
        if b.get("code") == DETERMINISTIC_CHECKS_MISSING_CODE or \
                _LEGACY_DETERMINISTIC_CHECKS_RE.search(b.get("reason") or ""):
            b["resolved"] = True
            b["code"] = DETERMINISTIC_CHECKS_MISSING_CODE
            resolved += 1
    if resolved:
        projstate.write_yaml(agentic_dir, "blockers.yaml", blockers)
    return resolved


def recover_bootstrap_deadlock(agentic_dir):
    """Self-healing for tasks blocked solely by the (now-fixed) zero-check
    deadlock -- including projects created before this fix existed, whose
    tasks predate the `kind` classification entirely. Never marks a task
    done, never touches worktrees -- it only clears the stale block (task
    status/blocking_reason AND every duplicate blocker record for it) so
    the next cycle re-attempts the task under the fixed gate. Returns one
    auditable event dict per recovered task."""
    if not projstate.exists(agentic_dir):
        return []
    backlog = projstate.load_backlog(agentic_dir)
    decisions = decisions_text(agentic_dir)
    earliest_id = _earliest_task_id(agentic_dir, backlog)
    events = []
    for task in backlog:
        if task["status"] != "blocked" or \
                not _is_legacy_deterministic_checks_block(task):
            continue
        eligible, _ = bootstrap_eligible(task, backlog, decisions)
        retag = False
        if not eligible and not task.get("kind") and \
                task["id"] == earliest_id:
            # a task that predates the `kind` classification entirely,
            # AND sits in the project's initial/scaffolding position:
            # probe what eligibility WOULD be if it were retroactively
            # tagged bootstrap -- only commit the retag if that actually
            # clears the bar (a recorded test-framework commitment still
            # has to exist somewhere; this never rescues a task that
            # simply has no evidence of one)
            probe = dict(task, kind=BOOTSTRAP_KIND)
            eligible, _ = bootstrap_eligible(probe, backlog, decisions)
            retag = eligible
        if not eligible:
            continue
        fields = {"status": "pending", "blocking_reason": None}
        if retag:
            fields["kind"] = BOOTSTRAP_KIND
        projstate.update_task(agentic_dir, task["id"], **fields)
        resolved = _resolve_deterministic_checks_blockers(agentic_dir,
                                                           task["id"])
        events.append({"task_id": task["id"], "retagged_kind": retag,
                       "resolved_blockers": resolved})
    return events


# -- recovery: the expected_paths-as-allowlist contract bug ---------------------

STRUCTURAL_CONTRACT_MISMATCH_CODE = \
    projstate.BLOCKER_CODE_STRUCTURAL_CONTRACT_MISMATCH

# Matches the exact historical failures this class of bug produced:
#  - run 20260722-181213: expected_paths wrongly enforced as a second
#    write allowlist ("... is outside task's expected_paths despite
#    being in allowed_paths ...") and/or no primitive for a bare
#    required directory ("Cannot create src/ directory through file
#    write operations").
#  - run 20260723-225324: a preserved task worktree from an earlier
#    (pre-fix) attempt held a file (".gitignore") outside a NEWER,
#    narrower work order's allowed_paths, and the worker had no way to
#    resolve it -- even a "delete" needs allowed_paths membership
#    ("Forbidden path .gitignore was touched during previous cycle;
#    WORKER role cannot edit paths outside allowed_paths ...", "cannot
#    create ... directory ... git init or mkdir on valid paths"). Fixed
#    by project.py's preserved-work scope-compatibility check, which
#    now resets an incompatible preserved worktree itself instead of
#    handing the model an unsolvable catch-22.
_LEGACY_EXPECTED_PATHS_CONTRACT_RE = re.compile(
    r"outside task.?s expected_paths|expected_paths.{0,40}despite.{0,40}"
    r"allowed_paths|cannot create[^.]{0,80}directory|"
    r"forbidden path .{0,80} touched|"
    r"cannot edit paths outside allowed_paths", re.I)


def _is_legacy_expected_paths_contract_block(task):
    return bool(_LEGACY_EXPECTED_PATHS_CONTRACT_RE.search(
        task.get("blocking_reason") or ""))


def _resolve_contract_blockers(agentic_dir, task_id):
    blockers = projstate.read_yaml(agentic_dir, "blockers.yaml",
                                   {"blockers": []})
    resolved = 0
    for b in blockers.get("blockers", []):
        if b.get("resolved") or b.get("task") != task_id:
            continue
        if b.get("code") == STRUCTURAL_CONTRACT_MISMATCH_CODE or \
                _LEGACY_EXPECTED_PATHS_CONTRACT_RE.search(
                    b.get("reason") or ""):
            b["resolved"] = True
            b["code"] = STRUCTURAL_CONTRACT_MISMATCH_CODE
            resolved += 1
    if resolved:
        projstate.write_yaml(agentic_dir, "blockers.yaml", blockers)
    return resolved


def _revert_preserved_worktree_if_present(agentic_dir, task_id):
    """Best-effort: if this task has a preserved per-task worktree left
    over from an earlier attempt, discard any uncommitted changes in it
    now, at recovery time, instead of counting on the NEXT cycle's own
    preserved-work compatibility check (project.py) to reach that code.
    That check only runs on an `action: execute` cycle -- if the
    conductor instead re-queues the task (observed live: run
    20260723-235604 against ollama-pilot, immediately after this exact
    recovery ran and reset the task to pending), the stale content is
    never reached and never cleaned, and the model faces the same
    unsolvable catch-22 again next time. Never raises: worktree cleanup
    is a courtesy here, not a safety boundary (the real boundary is
    `allowed_paths`, enforced fresh on every attempt regardless)."""
    from . import taskspace
    path = taskspace.task_worktree_path(agentic_dir, task_id)
    if not os.path.exists(os.path.join(path, ".git")):
        return False
    gitops.run_git(["reset", "--hard", "HEAD"], cwd=path, check=False)
    gitops.run_git(["clean", "-fd"], cwd=path, check=False)
    return True


def recover_expected_paths_contract_bug(agentic_dir):
    """Self-healing for tasks blocked by the pre-fix expected_paths
    contract mismatch (item 9): expected_paths was wrongly enforced as a
    second write allowlist, so a normal scaffold-support file (e.g.
    .gitignore) already inside allowed_paths could fail structural
    validation, and the worker had no primitive for a bare required
    directory. The bug was in the platform's own check (now fixed in
    `_check_expected_outputs`/the `mkdir` edit action) -- not a security
    boundary -- so recovery here is unconditional for any task matching
    the exact historical wording: reset to pending, resolve only the
    matching blocker(s), NEVER mark the task complete. The task's
    preserved worktree is reverted here too (see
    `_revert_preserved_worktree_if_present`) so the very next attempt
    starts clean regardless of what the conductor decides to do with
    it -- previously this relied entirely on project.py's own
    preserved-work compatibility check running on a later `execute`
    cycle, which never happens if the conductor re-queues instead."""
    if not projstate.exists(agentic_dir):
        return []
    backlog = projstate.load_backlog(agentic_dir)
    events = []
    for task in backlog:
        if task["status"] != "blocked" or \
                not _is_legacy_expected_paths_contract_block(task):
            continue
        projstate.update_task(agentic_dir, task["id"], status="pending",
                              blocking_reason=None)
        resolved = _resolve_contract_blockers(agentic_dir, task["id"])
        reverted = _revert_preserved_worktree_if_present(
            agentic_dir, task["id"])
        events.append({"task_id": task["id"], "resolved_blockers": resolved,
                       "worktree_reverted": reverted})
    return events
