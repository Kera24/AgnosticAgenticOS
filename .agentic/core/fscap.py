"""Safe, capability-based filesystem layer (Phase 1.D).

Named, worktree-scoped primitives every execution engine (native today,
Orca later) should use instead of touching the filesystem directly.
Every primitive is built on `gitops.safe_join`/`safe_makedirs`/
`ensure_parent_dir` -- the same hardened, realpath-based containment
check used everywhere else in the platform (traversal, UNC, drive
changes, reserved device names, symlink/junction escapes all rejected;
pre- AND post-operation containment checks). Nothing here uses a shell,
and nothing here lets a model run an arbitrary command -- only
`run_approved_check`, gated by the existing configured allowlist
(`execpolicy.run_allowlisted`).

Every primitive takes an optional `log` callback and emits a structured
audit event through it -- callers that don't care can omit it."""
import os
import re

from . import errors, execpolicy, gitops

_NOOP_LOG = lambda event: None   # noqa: E731

# A model self-reporting `blocked` may not be trusted blindly (item 5 of
# the canonical-contract-divergence fix): these primitives
# (create_directory/create_file/inspect_git_read_only above) are plain
# `os`-module operations, unconditionally available on every platform
# Python itself runs on -- no environment probing, no external tool, no
# feature negotiation is ever required to prove they exist. A claim that
# one of them is "unavailable" is therefore ALWAYS contradicted by this
# module's own existence, never a legitimate uncertainty.
_CONTRADICTED_CAPABILITY_PATTERNS = (
    ("create_directory", re.compile(
        r"cannot create (a |the )?(\S+\s+){0,3}?director|"
        r"director\w* creation (is |req\w*).{0,20}(unavailable|"
        r"not available|not possible)|"
        r"no (capability|primitive|way) to (create|make) (a )?director|"
        r"director(y|ies) requires? mkdir", re.I)),
    ("create_file", re.compile(
        r"cannot create (a |the )?file|file creation (is |req\w*)"
        r".{0,20}(unavailable|not available|not possible)", re.I)),
    ("git_init", re.compile(
        r"cannot (run |use )?git init|no (capability|way) to (run |use )?"
        r"git init|require mkdir/git init", re.I)),
    ("role_capability_fabrication", re.compile(
        r"\bworker role cannot\b|role cannot edit paths outside", re.I)),
)


def contradicted_capability_claim(text):
    """(capability_name) for the first ALWAYS-AVAILABLE platform
    primitive this module provides that a free-form blocked/failure
    claim falsely declares unavailable, or `None` when the claim matches
    none of the known contradiction patterns (i.e. it may be a genuine
    issue, never assumed false by omission)."""
    text = text or ""
    for name, pattern in _CONTRADICTED_CAPABILITY_PATTERNS:
        if pattern.search(text):
            return name
    return None


def _emit(log, event):
    (log or _NOOP_LOG)(dict(event, layer="fscap"))


def create_directory(worktree, rel_path, allowed_paths, protected,
                     authorised_exceptions=None, log=None):
    """Idempotent: creating an already-existing directory is a no-op."""
    full = gitops.safe_makedirs(worktree, rel_path, allowed_paths, protected,
                                authorised_exceptions=authorised_exceptions)
    _emit(log, {"event": "fscap_create_directory", "path": rel_path,
                "ok": True})
    return full


def create_file(worktree, rel_path, content, allowed_paths, forbidden_paths,
                protected, authorised_exceptions=None, overwrite=True,
                log=None):
    """Full-content file write; authorised parent directories are
    created automatically. Refuses to overwrite an existing file unless
    `overwrite=True` (default, matching the existing worker "write"
    action semantics)."""
    bad = gitops.check_paths([rel_path], allowed_paths, forbidden_paths,
                             protected,
                             authorised_exceptions=authorised_exceptions)
    if bad:
        raise errors.PolicyError("; ".join(bad))
    full = gitops.safe_join(worktree, rel_path)
    if os.path.exists(full) and not overwrite:
        raise errors.PolicyError("refusing to overwrite existing file: %s"
                                 % rel_path)
    gitops.ensure_parent_dir(worktree, full)
    with open(full, "w", encoding="utf-8", newline="") as fh:
        fh.write(content or "")
    _emit(log, {"event": "fscap_create_file", "path": rel_path,
                "bytes": len(content or "")})
    return full


def read_file(worktree, rel_path, log=None, max_bytes=2_000_000):
    """Read-only; still worktree-contained via `safe_join` (no
    escape-via-read either)."""
    full = gitops.safe_join(worktree, rel_path)
    if not os.path.isfile(full):
        raise errors.PolicyError("not a file: %s" % rel_path)
    with open(full, encoding="utf-8", errors="replace") as fh:
        content = fh.read(max_bytes)
    _emit(log, {"event": "fscap_read_file", "path": rel_path})
    return content


def modify_file(worktree, rel_path, content, allowed_paths, forbidden_paths,
                protected, authorised_exceptions=None, log=None):
    """Alias of `create_file(overwrite=True)` -- kept as a distinct name
    because "modify an existing file" and "create a new one" are
    different intents for audit/evidence purposes even though the
    underlying write mechanics are identical."""
    full = create_file(worktree, rel_path, content, allowed_paths,
                       forbidden_paths, protected,
                       authorised_exceptions=authorised_exceptions,
                       overwrite=True, log=log)
    _emit(log, {"event": "fscap_modify_file", "path": rel_path})
    return full


def rename_path(worktree, rel_src, rel_dst, allowed_paths, forbidden_paths,
                protected, authorised_exceptions=None, log=None):
    """Both source and destination are independently validated (pre AND
    post containment on the destination) -- a rename can never be used
    to smuggle a file outside the worktree or outside allowed_paths."""
    bad = gitops.check_paths([rel_src, rel_dst], allowed_paths,
                             forbidden_paths, protected,
                             authorised_exceptions=authorised_exceptions)
    if bad:
        raise errors.PolicyError("; ".join(bad))
    src_full = gitops.safe_join(worktree, rel_src)
    if not os.path.exists(src_full):
        raise errors.PolicyError("rename source does not exist: %s"
                                 % rel_src)
    dst_full = gitops.safe_join(worktree, rel_dst)
    gitops.ensure_parent_dir(worktree, dst_full)
    os.rename(src_full, dst_full)
    base = os.path.realpath(worktree)
    real_dst = os.path.realpath(dst_full)
    if not (real_dst == base or
            os.path.normcase(real_dst).startswith(
                os.path.normcase(base + os.sep))):
        raise errors.PolicyError(
            "rename destination escaped worktree after the operation: %s"
            % rel_dst)
    _emit(log, {"event": "fscap_rename_path", "from": rel_src,
                "to": rel_dst})
    return dst_full


def list_directory(worktree, rel_path=".", log=None):
    full = gitops.safe_join(worktree, rel_path)
    if not os.path.isdir(full):
        raise errors.PolicyError("not a directory: %s" % rel_path)
    entries = sorted(os.listdir(full))
    _emit(log, {"event": "fscap_list_directory", "path": rel_path,
                "count": len(entries)})
    return entries


# Read-only git subcommands only -- never a write/history-mutating
# operation, regardless of what the caller asks for.
_GIT_READ_ONLY_SUBCOMMANDS = ("status", "log", "diff", "show", "ls-files",
                              "rev-parse", "branch")


def inspect_git_read_only(worktree, args, log=None):
    if not args or args[0] not in _GIT_READ_ONLY_SUBCOMMANDS:
        raise errors.PolicyError(
            "git subcommand not read-only-approved: %r"
            % (args[0] if args else None))
    output = gitops.run_git(list(args), cwd=worktree, check=False)
    _emit(log, {"event": "fscap_inspect_git_read_only", "args": list(args)})
    return output


def run_approved_check(cmd, cwd, allowlist, timeout, env=None, log=None):
    """The ONLY way a model-originated command string ever executes:
    verbatim match against the configured allowlist, never a shell (see
    `execpolicy.run_allowlisted`). Returns None (and emits no audit
    event) when the command is not on the allowlist -- the caller must
    treat that as "skipped", never as "ran and passed"."""
    result = execpolicy.run_allowlisted(cmd, allowlist, cwd, timeout,
                                        env=env)
    if result is None:
        _emit(log, {"event": "fscap_run_approved_check_skipped",
                    "command": cmd})
        return None
    _emit(log, {"event": "fscap_run_approved_check", "command": cmd,
                "exit_code": result["exit_code"]})
    return result
