"""Git worktree isolation and path policy. Worker changes happen only inside
a per-run worktree; the user's own working tree is never touched. Path rules
(allowed / forbidden / protected) are enforced HERE in code — prompts are
advisory, this is the real boundary."""
import fnmatch
import os
import subprocess

from . import errors


def run_git(args, cwd, check=True):
    proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          text=True)
    if check and proc.returncode != 0:
        raise errors.ToolExecutionError("git %s failed: %s"
                                        % (" ".join(args), proc.stderr.strip()))
    return proc.stdout


def is_repo(root):
    proc = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                          cwd=root, capture_output=True, text=True)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def has_commits(root):
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                          capture_output=True, text=True)
    return proc.returncode == 0


def create_worktree(root, agentic_dir, run_id):
    """New branch + worktree under .agentic/worktrees/<run_id>. Requires at
    least one commit. Unrelated uncommitted user changes stay untouched in
    the main working tree."""
    if not has_commits(root):
        raise errors.PolicyError("repository has no commits; worktrees need HEAD")
    branch = "agentic/%s" % run_id
    path = os.path.join(agentic_dir, "worktrees", run_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    run_git(["worktree", "add", "-b", branch, path, "HEAD"], cwd=root)
    return path, branch


def remove_worktree(root, path, branch=None):
    run_git(["worktree", "remove", "--force", path], cwd=root, check=False)
    if branch:
        run_git(["branch", "-D", branch], cwd=root, check=False)


def stage_all(worktree):
    run_git(["add", "-A"], cwd=worktree)


def changed_files(worktree):
    out = run_git(["diff", "--cached", "--name-only"], cwd=worktree)
    return [line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()]


def changed_files_with_status(worktree):
    """[(path, status), ...] from the staged diff, e.g. [("src/a.py", "M")].
    Status is git's raw letter (A/M/D/R100/...) -- callers that only care
    about deletions should check `status.startswith("D")`."""
    out = run_git(["diff", "--cached", "--name-status"], cwd=worktree)
    pairs = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            pairs.append((parts[-1].strip().replace("\\", "/"), parts[0].strip()))
    return pairs


def changed_lines(worktree):
    out = run_git(["diff", "--cached", "--numstat"], cwd=worktree)
    total = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            for value in parts[:2]:
                if value.isdigit():
                    total += int(value)
    return total


def diff_text(worktree, max_chars=60000):
    out = run_git(["diff", "--cached"], cwd=worktree)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n... [diff truncated at %d chars]" % max_chars
    return out


def commit_all(worktree, message):
    stage_all(worktree)
    run_git(["-c", "user.name=agentic-os", "-c", "user.email=agentic-os@local",
             "commit", "-m", message, "--no-verify", "--allow-empty"],
            cwd=worktree)


# -- path policy -------------------------------------------------------------

def _norm(path):
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def match_pattern(path, pattern):
    path, pattern = _norm(path), _norm(pattern)
    if fnmatch.fnmatch(path, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
        return True
    if "/" not in pattern and fnmatch.fnmatch(os.path.basename(path), pattern):
        return True
    return False


def matches_any(path, patterns):
    return any(match_pattern(path, p) for p in patterns or [])


# Universal, non-source tool-generated artefacts that appear as a SIDE
# EFFECT of running an approved deterministic check inside the worktree
# (python -m pytest -q writes __pycache__/*.pyc and .pytest_cache/ as it
# runs; npm/node likewise write into node_modules/.cache) -- never
# something the worker itself chose to write, and never a legitimate
# scope-violation signal. A properly-scaffolded project's own .gitignore
# would normally keep git from ever staging these; this exclusion is the
# same rule applied unconditionally, so a project without one yet (e.g.
# a bootstrap task, before .gitignore itself has been written) can never
# have a check's own output turned into a false scope-violation block on
# the NEXT repair attempt in the same worktree.
TOOL_ARTIFACT_PATTERNS = (
    "__pycache__/**", "**/__pycache__/**", "*.pyc", "**/*.pyc",
    "*.pyo", "**/*.pyo",
    ".pytest_cache/**", "**/.pytest_cache/**",
    "node_modules/**", "**/node_modules/**",
    ".DS_Store", "**/.DS_Store",
)


def is_tool_artifact(path):
    return matches_any(path, TOOL_ARTIFACT_PATTERNS)


def filter_tool_artifacts(paths):
    """Drops universal tool-generated artefacts from a changed-files
    list before it reaches the scope-violation check (or any file/line
    count) -- see `TOOL_ARTIFACT_PATTERNS`. Never widens what a worker
    may WRITE (allowed_paths is untouched); only prevents a check's own
    unavoidable side effects from being mistaken for an unauthorised
    edit."""
    return [p for p in paths if not is_tool_artifact(p)]


def load_protected_paths(cfg, agentic_dir):
    patterns = []
    guard = os.path.join(agentic_dir, "guardrails", "protected-paths.txt")
    if os.path.exists(guard):
        with open(guard, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    patterns.extend((cfg.get("contract", {}) or {}).get("extra_protected_paths") or [])
    return patterns


# A fixed, reviewed allowlist -- never a generic "the plan says so" escape
# hatch. A Capability Plan can only narrow these two specific categories,
# and only once it has actually selected the capability that needs them
# (Phase 0 decision: option 1 -- see capability-intelligence-design.md
# section 3). Every other protected pattern (.env*, secrets, auth,
# payments, workflows, ...) is never authorisable this way.
_CAPABILITY_PATH_EXCEPTIONS = (
    ("supabase/migrations/**", ("supabase", "database_migrations")),
    ("Dockerfile", ("docker",)),
    ("docker-compose.yml", ("docker",)),
    ("docker-compose.yaml", ("docker",)),
)


def capability_authorised_exceptions(capability_plan):
    """Concrete path globs a project's own CapabilityPlan (Phase 3)
    authorises, narrowing -- never widening -- the protected-paths list.
    Returns [] for no plan (default: nothing is authorised)."""
    if not capability_plan:
        return []
    selected = {r.get("capability_id") for r in
               (capability_plan.get("required_capabilities") or [])
               + (capability_plan.get("optional_capabilities") or [])}
    return [pattern for pattern, needs in _CAPABILITY_PATH_EXCEPTIONS
           if selected & set(needs)]


def pattern_is_protected(pattern, protected, authorised_exceptions=None):
    """True if `pattern` (typically a work-order `allowed_paths` glob)
    would grant access to a protected path, unless it is fully covered
    by an authorised exception."""
    hits = matches_any(pattern, protected) or \
        any(match_pattern(pp, pattern) for pp in protected)
    if not hits:
        return False
    return not matches_any(pattern, authorised_exceptions or [])


def check_paths(paths, allowed, forbidden, protected,
                authorised_exceptions=None):
    """Return list of violation strings; empty means compliant. An empty
    allowed list rejects everything (deny by default)."""
    violations = []
    for path in paths:
        p = _norm(path)
        if matches_any(p, protected) and \
                not matches_any(p, authorised_exceptions or []):
            violations.append("protected path touched: %s" % p)
        elif matches_any(p, forbidden):
            violations.append("forbidden path touched: %s" % p)
        elif not matches_any(p, allowed):
            violations.append("path outside allowed_paths: %s" % p)
    return violations


def safe_join(worktree, rel_path):
    """Resolve rel_path inside worktree, rejecting traversal/absolute/UNC
    paths. `os.path.realpath` resolves every symlink/junction in the
    chain (including intermediate components), so an escape hidden behind
    a symlinked directory is caught by the containment check below just
    like a literal `../`. The comparison is case-normalised (on Windows,
    `normcase` also folds `/` to `\\`) so a same-target, different-case
    path can never slip past the containment check on a case-insensitive
    filesystem."""
    rel = rel_path.replace("\\", "/")
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise errors.PolicyError("absolute path rejected: %s" % rel_path)
    full = os.path.realpath(os.path.join(worktree, rel))
    base = os.path.realpath(worktree)
    full_cmp = os.path.normcase(full)
    base_with_sep_cmp = os.path.normcase(base + os.sep)
    if not full_cmp.startswith(base_with_sep_cmp) and \
            full_cmp != os.path.normcase(base):
        raise errors.PolicyError("path escapes worktree: %s" % rel_path)
    return full


# Windows reserved device names -- never a legitimate directory/file
# component regardless of extension (CON, CON.txt, con/sub, ... are all
# reserved). Rejected outright by `safe_makedirs`.
_RESERVED_DEVICE_NAMES = ({"CON", "PRN", "AUX", "NUL"} |
                          {"COM%d" % i for i in range(1, 10)} |
                          {"LPT%d" % i for i in range(1, 10)})


def _reject_reserved_components(rel):
    for part in rel.split("/"):
        if not part:
            continue
        name = part.split(".", 1)[0].upper()
        if name in _RESERVED_DEVICE_NAMES:
            raise errors.PolicyError(
                "reserved device name in path: %s" % rel)


def directory_is_allowed(rel_dir, allowed):
    """True if creating `rel_dir` is covered by `allowed_paths` -- either
    a glob covering files under it (e.g. "src/**" authorises creating
    "src"), or the directory itself listed literally (with or without a
    trailing slash)."""
    rel_dir = _norm(rel_dir).rstrip("/")
    if not rel_dir or rel_dir == ".":
        return True
    probe = rel_dir + "/.__agentic_probe__"
    return any(match_pattern(probe, p) or match_pattern(rel_dir, p) or
              match_pattern(rel_dir + "/", p) for p in allowed or [])


def safe_makedirs(worktree, rel_dir, allowed, protected,
                  authorised_exceptions=None):
    """The one sanctioned directory-creation primitive (item 5 of the
    bootstrap-contract fix): creates `rel_dir` (and any parents) recursively,
    ONLY inside the current worktree, ONLY when `allowed_paths` actually
    covers it. Pure `os.makedirs` -- never spawns a shell process anywhere
    in this call chain. Idempotent -- creating an already-existing directory
    is a no-op, never an error. Rejects absolute paths, drive letters, UNC
    paths, traversal, symlink/junction escapes (via `safe_join`'s realpath
    containment check) and reserved device names. The resulting path is
    validated with `safe_join` both BEFORE and AFTER the actual
    `os.makedirs` call -- a directory that appears to have escaped after
    creation is treated as a policy violation, not silently accepted."""
    # absolute/drive/UNC rejection MUST happen before any stripping --
    # stripping leading slashes first would silently turn "\\server\share"
    # into the harmless-looking relative "server/share", rejecting nothing
    # instead of raising.
    unslashed = rel_dir.replace("\\", "/")
    if unslashed.startswith("/") or (len(unslashed) > 1 and
                                     unslashed[1] == ":"):
        raise errors.PolicyError(
            "absolute/UNC path rejected: %s" % rel_dir)
    rel = unslashed.strip("/")
    if not rel:
        raise errors.PolicyError("empty directory path")
    _reject_reserved_components(rel)
    if matches_any(rel, protected) and \
            not matches_any(rel, authorised_exceptions or []):
        raise errors.PolicyError("directory is a protected path: %s" % rel)
    if not directory_is_allowed(rel, allowed):
        raise errors.PolicyError("directory outside allowed_paths: %s" % rel)
    full_before = safe_join(worktree, rel)
    os.makedirs(full_before, exist_ok=True)
    full_after = safe_join(worktree, rel)
    if not os.path.isdir(full_after):
        raise errors.PolicyError(
            "directory creation failed post-creation validation: %s" % rel)
    return full_after


def ensure_parent_dir(worktree, full_path):
    """Create the parent directory of an ALREADY safe_join-validated file
    path. The file path itself proved authorisation (it passed
    `check_paths` against `allowed_paths` before `safe_join` produced
    `full_path`), so this only needs the worktree-containment check --
    re-applied before AND after `os.makedirs`, exactly like
    `safe_makedirs` -- never a second, potentially-mismatched
    allowed_paths glob check against the bare directory."""
    parent = os.path.dirname(full_path)
    base = os.path.realpath(worktree)
    if not parent or os.path.normcase(parent) == os.path.normcase(base):
        return base
    if not os.path.normcase(parent).startswith(
            os.path.normcase(base + os.sep)):
        raise errors.PolicyError("parent directory escapes worktree")
    os.makedirs(parent, exist_ok=True)
    real_parent = os.path.realpath(parent)
    if not os.path.normcase(real_parent).startswith(
            os.path.normcase(base + os.sep)):
        raise errors.PolicyError(
            "parent directory escaped worktree after creation")
    return real_parent
