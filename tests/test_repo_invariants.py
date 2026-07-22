"""Repository invariant: runtime-generated machine-local state (auth
verification results, model registry snapshots, machine config,
credentials/tokens/sessions, decision logs, usage/capacity ledgers) must
never be able to enter the git index or a distributable package -- not
just the two files that were caught untracked in practice
(.agentic/memory/auth-verification.json, .agentic/memory/model-registry.json)
but ANY future file written under .agentic/memory/, via a blanket
.gitignore rule rather than an ever-growing enumerated list."""
import subprocess

import pytest

from conftest import REPO


def _check_ignore(paths):
    """[(path, ignored: bool), ...] -- `git check-ignore` needs no file to
    actually exist; it is pure path/pattern matching against .gitignore."""
    proc = subprocess.run(["git", "check-ignore", "--verbose", "--non-matching"]
                          + list(paths), cwd=str(REPO), capture_output=True,
                          text=True)
    out = {}
    for line in proc.stdout.splitlines():
        # matched:  ".gitignore:23:.agentic/memory/*\t<path>"
        # unmatched: "::\t<path>"
        rule, _, path = line.partition("\t")
        out[path] = not rule.startswith("::")
    return out


KNOWN_RUNTIME_FILES = [
    ".agentic/memory/auth-verification.json",
    ".agentic/memory/model-registry.json",
    ".agentic/config.machine.yaml",
]

HYPOTHETICAL_RUNTIME_FILES = [
    ".agentic/memory/credentials.json",
    ".agentic/memory/tokens.json",
    ".agentic/memory/session.json",
    ".agentic/memory/decisions.jsonl",
    ".agentic/memory/usage.tsv",
    ".agentic/memory/some-new-ledger-nobody-has-invented-yet.db",
]


@pytest.mark.parametrize("path", KNOWN_RUNTIME_FILES)
def test_known_runtime_artifacts_are_gitignored(path):
    """The exact two files caught untracked live (plus config.machine.yaml,
    the other historically-known runtime file) must be ignored."""
    result = _check_ignore([path])
    assert result.get(path) is True, (
        "%r is NOT covered by .gitignore -- it could be accidentally "
        "`git add`ed" % path)


@pytest.mark.parametrize("path", HYPOTHETICAL_RUNTIME_FILES)
def test_any_future_memory_artifact_is_gitignored(path):
    """The blanket `.agentic/memory/*` rule (not an enumerated list) must
    cover files that don't exist yet -- credentials, tokens, sessions,
    decision logs, ledgers, whatever gets written next."""
    result = _check_ignore([path])
    assert result.get(path) is True, (
        "%r is not covered by the blanket .agentic/memory/* rule" % path)


def test_memory_readme_template_still_tracked():
    """The blanket rule must not accidentally swallow the one template
    file that IS meant to ship in git."""
    result = _check_ignore([".agentic/memory/README.md"])
    assert result.get(".agentic/memory/README.md") is False


def test_known_runtime_files_are_not_currently_tracked():
    """Defence in depth: even if .gitignore had a gap, these must not
    already be sitting in the index from before the rule existed."""
    proc = subprocess.run(["git", "ls-files"] + KNOWN_RUNTIME_FILES,
                          cwd=str(REPO), capture_output=True, text=True)
    tracked = [line for line in proc.stdout.splitlines() if line.strip()]
    assert tracked == [], "runtime file(s) already tracked in git: %s" % tracked


def test_package_command_excludes_the_memory_directory():
    """`cmd_package` (agentic-os-dist.zip) only ever walks `git ls-files`
    (tracked files) and additionally excludes the .agentic/memory/ prefix
    explicitly -- belt-and-braces on top of .gitignore."""
    run_path = REPO / ".agentic" / "run"
    text = run_path.read_text(encoding="utf-8")
    assert '".agentic/memory/"' in text
