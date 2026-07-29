"""The explicit project-review command honours --project overlays."""
import runpy
from pathlib import Path
from types import SimpleNamespace

from core import project


def test_project_review_applies_registered_project_overlay(monkeypatch,
                                                           capsys):
    namespace = runpy.run_path(str(
        Path(__file__).parents[1] / ".agentic" / "run"))
    command = namespace["cmd_project_review"]
    command.__globals__["_apply_project_overlay"] = (
        lambda cfg, args: ({"runtime": {"project_dir": "selected"}},
                           "selected-memory"))
    command.__globals__["_overrides"] = lambda args: {}
    observed = {}
    monkeypatch.setattr(
        project, "final_audit",
        lambda cfg, overrides=None: (
            observed.setdefault("cfg", cfg) or {"status": "complete"}))

    result = command({}, SimpleNamespace(project_id="ollama-pilot"))

    assert result == 0
    assert observed["cfg"]["runtime"]["project_dir"] == "selected"
    assert '"status": "complete"' in capsys.readouterr().out
