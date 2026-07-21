"""Real, deterministic, file/content-based checks for a small subset of
the taxonomy's `validation_checks` names (Phase 10) -- genuinely
runnable with no new dependencies, no model call, no network. Not every
validation-check name in the taxonomy has an implementation here: a real
accessibility scanner, an RLS-policy analyser, or a migration-
reproducibility runner is out of scope for this phase. An unimplemented
check is reported honestly as `implemented: False` -- never silently
skipped and never faked as passing.
"""
import os
import re

CHECK_IMPLEMENTATIONS = {}


def register(name):
    def deco(fn):
        CHECK_IMPLEMENTATIONS[name] = fn
        return fn
    return deco


def _walk_files(worktree, extensions):
    for base, dirs, names in os.walk(worktree):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules",
                                                 ".venv", "__pycache__")]
        for name in names:
            if name.lower().endswith(extensions):
                yield os.path.join(base, name)


@register("sitemap_present")
def _sitemap_present(worktree):
    return any(os.path.exists(os.path.join(worktree, d, "sitemap.xml"))
              for d in ("", "public", "static", "dist", "build"))


@register("robots_present")
def _robots_present(worktree):
    return any(os.path.exists(os.path.join(worktree, d, "robots.txt"))
              for d in ("", "public", "static", "dist", "build"))


@register("meta_titles_present")
def _meta_titles_present(worktree):
    files = list(_walk_files(worktree, (".html", ".htm")))
    if not files:
        return False
    title_re = re.compile(r"<title>.+?</title>", re.IGNORECASE | re.DOTALL)
    for path in files[:20]:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return False
        if not title_re.search(text):
            return False
    return True


@register("readme_present")
def _readme_present(worktree):
    return any(os.path.exists(os.path.join(worktree, n)) for n in
              ("README.md", "README.rst", "README.txt", "README"))


@register("env_example_present")
def _env_example_present(worktree):
    uses_env = False
    for path in _walk_files(worktree, (".py", ".js", ".ts", ".mjs")):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if "os.environ" in text or "process.env" in text:
            uses_env = True
            break
    if not uses_env:
        return True
    return any(os.path.exists(os.path.join(worktree, n)) for n in
              (".env.example", ".env.sample"))


def run_check(name, worktree):
    """Returns (implemented: bool, passed: bool-or-None, detail: str)."""
    fn = CHECK_IMPLEMENTATIONS.get(name)
    if fn is None:
        return False, None, ("no deterministic implementation for check "
                             "%r yet" % name)
    try:
        ok = bool(fn(worktree))
    except Exception as exc:   # noqa: BLE001
        return True, False, "check %r raised: %s" % (name, exc)
    return True, ok, "check %r %s" % (name, "passed" if ok else "failed")


def run_capability_checks(check_names, worktree):
    results = []
    for name in check_names:
        implemented, passed, detail = run_check(name, worktree)
        results.append({"name": name, "implemented": implemented,
                        "passed": passed, "detail": detail})
    return results
