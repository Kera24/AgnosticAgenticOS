"""Phase 1 -- canonical project.md: frontmatter + section parsing,
missing-information classification, safe-default inference, and
backward compatibility with existing plan.md files. Pure functions only
-- no I/O, no model calls."""
import pytest

from conftest import AGENTIC_SRC
from core.projectspec import (SECTION_SPECS, parse_project_spec,
                              render_template)
from core.schema import load_schema, validate

COMPLETE_DOC = """---
agentic_project_version: 1
name: Boutique Hotel Site
project_type: web_application
priority: 60
autonomy: milestone_review
deployment_authorised: false
remote_push_authorised: false
---

## Product Vision

A marketing website for a boutique hotel in Kathmandu.

## Problem

The hotel has no online presence and loses bookings to aggregators.

## Target Users

Prospective guests searching for boutique hotels in Kathmandu.

## User Roles

Visitor, admin (manages room listings).

## Functional Requirements

- Show room listings with photos and pricing.
- Let visitors submit a booking enquiry.
- Admin can edit room listings.

## User Journeys

Visitor lands on homepage, browses rooms, submits enquiry.

## Design Direction

Warm, minimal, photography-led.

## Technical Preferences

No preference; choose sensible defaults.

## Integrations

None yet.

## Data and Privacy

Enquiry form collects name/email/phone only.

## Security Requirements

Standard practice.

## SEO and Discoverability

Must rank for "boutique hotel Kathmandu".

## Accessibility

WCAG AA.

## Environments

Local only for now.

## Testing Expectations

Standard automated tests.

## Deployment

Not yet.

## Constraints

None.

## Acceptance Criteria

- Booking enquiry form submits successfully.
- Room listings render with photos.

## Autonomous Permissions

Default platform boundaries.

## Protected Actions

None beyond platform defaults.
"""


def test_complete_specification_has_no_assumptions_or_blockers():
    spec = parse_project_spec(COMPLETE_DOC)
    assert spec["assumptions"] == []
    assert spec["blocking_questions"] == []
    assert spec["warnings"] == []
    assert spec["frontmatter"]["name"] == "Boutique Hotel Site"
    assert spec["frontmatter"]["priority"] == 60
    assert spec["sections"]["Product Vision"]["present"] is True
    assert "boutique hotel" in spec["sections"]["Product Vision"]["content"]
    schema = load_schema(str(AGENTIC_SRC / "schemas" /
                             "project-specification.schema.json"))
    assert validate(spec, schema) == []


def test_incomplete_specification_classifies_every_missing_section():
    doc = "## Product Vision\n\nA todo app.\n\n" \
          "## Functional Requirements\n\n- Add a todo.\n"
    spec = parse_project_spec(doc)
    assert spec["blocking_questions"] == []   # both blocking sections present
    present_sections = {"Product Vision", "Functional Requirements"}
    for a in spec["assumptions"]:
        assert a["section"] not in present_sections
        assert a["classification"] in ("optional", "safely_inferable",
                                       "important_but_non_blocking")
    # every non-blocking, absent section produced exactly one assumption
    expected_missing = set(SECTION_SPECS) - present_sections
    assert {a["section"] for a in spec["assumptions"]} == expected_missing


def test_invalid_frontmatter_falls_back_to_platform_defaults():
    doc = "---\nname: [unterminated\n---\n\n## Product Vision\n\nX\n"
    spec = parse_project_spec(doc)
    assert any("invalid YAML" in w for w in spec["warnings"])
    assert spec["frontmatter"]["project_type"] == "web_application"
    assert spec["frontmatter"]["deployment_authorised"] is False


def test_invalid_frontmatter_value_falls_back_with_warning():
    doc = "---\nautonomy: full_auto_yolo\n---\n\n## Product Vision\n\nX\n"
    spec = parse_project_spec(doc)
    assert spec["frontmatter"]["autonomy"] == "completion_only"
    assert any("autonomy" in w for w in spec["warnings"])


def test_duplicate_sections_use_first_occurrence_and_warn():
    doc = ("## Product Vision\n\nFirst version.\n\n"
          "## Functional Requirements\n\n- thing\n\n"
          "## Product Vision\n\nSecond version -- should be ignored.\n")
    spec = parse_project_spec(doc)
    assert spec["sections"]["Product Vision"]["content"] == "First version."
    assert any("duplicate section 'Product Vision'" in w
              for w in spec["warnings"])


def test_safely_inferred_defaults_never_invent_specifics():
    doc = "## Product Vision\n\nX\n\n## Functional Requirements\n\n- Y\n"
    spec = parse_project_spec(doc)
    env = next(a for a in spec["assumptions"] if a["section"] == "Environments")
    assert env["classification"] == "safely_inferable"
    assert "local" in env["value"].lower()
    perms = next(a for a in spec["assumptions"]
                if a["section"] == "Autonomous Permissions")
    assert "contract.md" in perms["value"] or "platform" in perms["value"].lower()
    # never invents a credential, price, or production target
    joined = " ".join(a["value"] for a in spec["assumptions"]).lower()
    for forbidden in ("api key", "$", "production url", "password"):
        assert forbidden not in joined


def test_materially_blocking_ambiguity_is_a_question_not_an_assumption():
    doc = "## Design Direction\n\nMinimal.\n"   # no vision, no requirements
    spec = parse_project_spec(doc)
    blocking_sections = {q["section"] for q in spec["blocking_questions"]}
    assert blocking_sections == {"Product Vision", "Functional Requirements"}
    for q in spec["blocking_questions"]:
        assert q["question"]
    # blocking sections must never also appear as assumptions
    assumed_sections = {a["section"] for a in spec["assumptions"]}
    assert blocking_sections.isdisjoint(assumed_sections)


def test_windows_line_endings_parse_identically_to_unix():
    unix_doc = "## Product Vision\r\n\r\nA thing.\r\n\r\n" \
              "## Functional Requirements\r\n\r\n- Do X.\r\n"
    spec = parse_project_spec(unix_doc)
    assert spec["sections"]["Product Vision"]["content"] == "A thing."
    assert spec["blocking_questions"] == []


def test_unicode_content_is_preserved_verbatim():
    doc = ("## Product Vision\n\n"
          "काठमाडौंको लागि एउटा होटल वेबसाइट — épicerie café 日本語.\n\n"
          "## Functional Requirements\n\n- 予約フォーム\n")
    spec = parse_project_spec(doc)
    assert "काठमाडौं" in spec["sections"]["Product Vision"]["content"]
    assert "épicerie" in spec["sections"]["Product Vision"]["content"]
    assert "予約フォーム" in spec["sections"]["Functional Requirements"]["content"]


def test_existing_plan_md_with_no_structure_still_parses():
    """Backward compatibility: today's plan.md is free-form prose with no
    frontmatter and no recognised headers. It must never fail to parse --
    it becomes a specification with every section unresolved, but
    `raw_text` is preserved verbatim so the existing architect prompt
    (which is fed the raw plan text today) keeps working unchanged."""
    legacy_plan = "# My App\n\nDescribe the application to build here.\n"
    spec = parse_project_spec(legacy_plan)
    assert spec["raw_text"] == legacy_plan
    assert spec["frontmatter"]["project_type"] == "web_application"
    blocking_sections = {q["section"] for q in spec["blocking_questions"]}
    assert blocking_sections == {"Product Vision", "Functional Requirements"}
    assert spec["warnings"] == []   # absence of structure is not an error


def test_extra_custom_sections_are_preserved_not_discarded():
    doc = "## Product Vision\n\nX\n\n## Functional Requirements\n\n- Y\n\n" \
          "## Notes For Future Me\n\nRemember to check the domain.\n"
    spec = parse_project_spec(doc)
    assert "Notes For Future Me" in spec["extra_sections"]
    assert "domain" in spec["extra_sections"]["Notes For Future Me"]


def test_source_line_references_point_at_original_file():
    doc = "line0\n---\nname: X\n---\n\n## Product Vision\n\nHello.\n"
    spec = parse_project_spec(doc)
    start = spec["sections"]["Product Vision"]["start_line"]
    lines = doc.split("\n")
    assert "Product Vision" in lines[start - 1]


# -- template generator ------------------------------------------------------------

def test_render_template_covers_every_section_and_round_trips():
    template = render_template(name="Test App")
    for section in SECTION_SPECS:
        assert ("## %s" % section) in template
    assert "agentic_project_version: 1" in template
    assert "Test App" in template
    # the template itself parses cleanly (materially blocking sections are
    # commented placeholders, so they're still "absent" -- but it must not
    # raise and must not falsely mark them present)
    spec = parse_project_spec(template)
    assert spec["sections"]["Product Vision"]["present"] is False
    assert any(q["section"] == "Product Vision"
              for q in spec["blocking_questions"])


def test_render_template_never_mentions_internal_terminology():
    template = render_template().lower()
    for internal_term in ("skillreg", "mcp server", "context broker",
                          "capability resolver", "modelres"):
        assert internal_term not in template


# -- projectops integration ---------------------------------------------------------

def test_load_specification_uses_find_plan(tmp_path):
    from core import projectops
    root = tmp_path / "app"
    root.mkdir()
    (root / "plan.md").write_text(
        "## Product Vision\n\nX\n\n## Functional Requirements\n\n- Y\n",
        encoding="utf-8")
    record = {"root_path": str(root), "plan_path": "plan.md"}
    spec = projectops.load_specification(record)
    assert spec is not None
    assert spec["blocking_questions"] == []


def test_load_specification_returns_none_without_a_plan_file(tmp_path):
    from core import projectops
    root = tmp_path / "empty"
    root.mkdir()
    record = {"root_path": str(root), "plan_path": "plan.md"}
    assert projectops.load_specification(record) is None


def test_project_create_auto_generates_the_full_template(tmp_path,
                                                          monkeypatch):
    """Both `project create` (CLI) and the dashboard's add_project(...,
    create=True) share one code path that now writes the full commented
    template instead of a one-line placeholder -- Primary Product Outcome
    step 3 ("add a structured project.md") should need no manual work."""
    monkeypatch.setenv("AGENTIC_HOME", str(tmp_path / "home"))
    from core.registry import ProjectRegistry
    from ui import portfolio
    monkeypatch.setattr(portfolio, "_registry",
                        lambda: ProjectRegistry(home=str(tmp_path / "home")))
    root = tmp_path / "apps" / "new-app"
    record = portfolio.add_project(None, "new-app", str(root), create=True)
    plan_text = (root / record["plan_path"]).read_text(encoding="utf-8")
    for section in SECTION_SPECS:
        assert ("## %s" % section) in plan_text
    spec = parse_project_spec(plan_text)
    assert {q["section"] for q in spec["blocking_questions"]} == \
        {"Product Vision", "Functional Requirements"}
