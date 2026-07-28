"""Regression tests for credential scanning of unified diffs."""
from core.redact import looks_like_secret_in_diff


def _token():
    # Construct at runtime so this test file does not itself contain a
    # credential-shaped literal.
    return "sk-" + ("A" * 20)


def test_added_secret_is_rejected():
    patch = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-value = 'safe'\n"
        "+value = '" + _token() + "'\n"
    )
    assert looks_like_secret_in_diff(patch) is True


def test_removed_secret_does_not_block_safe_replacement():
    patch = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-value = '" + _token() + "'\n"
        "+value = 'safe'\n"
    )
    assert looks_like_secret_in_diff(patch) is False


def test_context_secret_does_not_block_unrelated_addition():
    patch = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " value = '" + _token() + "'\n"
        "+enabled = True\n"
    )
    assert looks_like_secret_in_diff(patch) is False


def test_diff_header_is_not_treated_as_added_content():
    patch = (
        "diff --git a/" + _token() + " b/" + _token() + "\n"
        "--- a/old.py\n"
        "+++ b/" + _token() + "\n"
        "@@ -0,0 +1 @@\n"
        "+enabled = True\n"
    )
    assert looks_like_secret_in_diff(patch) is False
