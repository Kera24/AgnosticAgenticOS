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
    """Resolve rel_path inside worktree, rejecting traversal/absolute paths."""
    rel = rel_path.replace("\\", "/")
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise errors.PolicyError("absolute path rejected: %s" % rel_path)
    full = os.path.realpath(os.path.join(worktree, rel))
    base = os.path.realpath(worktree)
    if not full.startswith(base + os.sep) and full != base:
        raise errors.PolicyError("path escapes worktree: %s" % rel_path)
    return full
