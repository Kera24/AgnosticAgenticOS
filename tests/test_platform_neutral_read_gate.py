"""Safe cross-platform evaluation of read-only documentation checks."""
from core import gate


def test_cat_relative_file_is_evaluated_without_shell(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("# App\n\nAll Active Completed\n", encoding="utf-8")
    cfg = {
        "verification": {"commands": [{
            "name": "readme", "command": "cat README.md",
            "mandatory": True, "kind": "structural",
        }]},
        "execution": {"command_timeout_seconds": 30},
    }

    result = gate.run_checks(cfg, str(tmp_path))

    assert result["ok"] is True
    assert result["results"][0]["platform_neutral"] is True
    assert "All Active Completed" in result["results"][0]["detail"]


def test_cat_internal_check_rejects_options_and_traversal(tmp_path):
    assert gate._platform_neutral_read_check(
        "cat -n README.md", str(tmp_path)) is None
    assert gate._platform_neutral_read_check(
        "cat ../outside.txt", str(tmp_path)) is None
    assert gate._platform_neutral_read_check(
        "cat one.txt two.txt", str(tmp_path)) is None


def test_cat_internal_check_reports_missing_file(tmp_path):
    assert gate._platform_neutral_read_check(
        "cat README.md", str(tmp_path)) == (
            False, "file 'README.md' does not exist")
