"""Recovery preserves source-plan acceptance semantics."""
from conftest import project_cfg, seed_project, simple_task
from core import projstate, recovery


def test_recovery_removes_unrequested_file_url_constraint(sandbox):
    project_cfg(sandbox)
    seed_project(sandbox, [simple_task()])
    agentic = str(sandbox["agentic"])
    projstate.write_text(agentic, "PROJECT.md", """# Project Plan

## Acceptance criteria

- The application opens locally in a browser.
""")
    projstate.write_yaml(agentic, "acceptance-criteria.yaml", {
        "requirements_map": [],
        "completion_criteria": [
            "All checks pass",
            "Application loads locally at file:// URL and renders task list",
        ],
    })

    events = recovery.recover_over_specific_file_url_criterion(agentic)

    assert events[0]["action"] == (
        "restore_source_plan_local_browser_criterion")
    criteria = projstate.read_yaml(
        agentic, "acceptance-criteria.yaml", {})
    assert criteria["completion_criteria"] == [
        "All checks pass",
        "The application opens locally in a browser.",
    ]


def test_recovery_preserves_explicit_file_url_requirement(sandbox):
    project_cfg(sandbox)
    seed_project(sandbox, [simple_task()])
    agentic = str(sandbox["agentic"])
    projstate.write_text(
        agentic, "PROJECT.md",
        "The application must open from a file:// URL.")
    projstate.write_yaml(agentic, "acceptance-criteria.yaml", {
        "completion_criteria": [
            "Application loads locally at file:// URL and renders task list",
        ],
    })

    assert recovery.recover_over_specific_file_url_criterion(agentic) == []
