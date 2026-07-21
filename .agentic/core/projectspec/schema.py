"""Declarative section table for the canonical project.md format.

Nothing here executes I/O or calls a model -- `parser.py` and
`template.py` both read this table so the set of recognised sections,
their missing-information classification, and their safe-default
inference stay in exactly one place.
"""

SCHEMA_VERSION = 1

# Classification of what happens when a section is absent or blank.
# - optional: the section adds detail but nothing depends on it; leave
#   empty, the Requirements Intelligence Engine (Phase 3) infers
#   capabilities from project_type/other sections regardless.
# - safely_inferable: a conservative, documented default is filled in and
#   recorded as an assumption; never invented (rule: never invent
#   credentials/legal requirements/prices/production targets).
# - important_but_non_blocking: work proceeds with a recorded assumption,
#   but the gap is worth the user's early attention.
# - materially_blocking: without this, nothing safe can be built; the
#   user is asked before the architect runs.
CLASSIFICATIONS = ("optional", "safely_inferable",
                    "important_but_non_blocking", "materially_blocking")

# section title (as it appears after "## ") -> spec
SECTION_SPECS = {
    "Product Vision": {
        "classification": "materially_blocking",
        "question": "What is this application, in one or two sentences? "
                    "What problem does it solve and for whom?",
    },
    "Problem": {
        "classification": "important_but_non_blocking",
        "default": "Not stated separately; inferred from Product Vision.",
    },
    "Target Users": {
        "classification": "important_but_non_blocking",
        "default": "Not stated; assumed to be a single general user "
                   "audience unless Functional Requirements imply roles.",
    },
    "User Roles": {
        "classification": "safely_inferable",
        "default": "Single implicit user role (no multi-tenant or "
                   "role-based access implied).",
    },
    "Functional Requirements": {
        "classification": "materially_blocking",
        "question": "What must the application actually let a user do? "
                    "A short bullet list of concrete capabilities is "
                    "enough.",
    },
    "User Journeys": {
        "classification": "safely_inferable",
        "default": "Not stated; derived from Functional Requirements "
                   "during requirements analysis.",
    },
    "Design Direction": {
        "classification": "optional",
        "default": "No specific design direction stated; a clean, "
                   "accessible default design system is used.",
    },
    "Technical Preferences": {
        "classification": "safely_inferable",
        "default": "Not stated; inferred from a repository scan and "
                   "sensible ecosystem defaults for the project type.",
    },
    "Integrations": {
        "classification": "optional",
        "default": "No third-party integrations stated.",
    },
    "Data and Privacy": {
        "classification": "important_but_non_blocking",
        "default": "No data/privacy requirements stated; treated as "
                   "handling no sensitive personal data until told "
                   "otherwise -- this assumption is never treated as a "
                   "compliance determination.",
    },
    "Security Requirements": {
        "classification": "important_but_non_blocking",
        "default": "No requirements stated beyond the platform's "
                   "always-applied security baseline.",
    },
    "SEO and Discoverability": {
        "classification": "optional",
        "default": "Not stated; a baseline technical-SEO capability is "
                   "still inferred for any public-facing project type.",
    },
    "Accessibility": {
        "classification": "optional",
        "default": "Not stated; a baseline accessibility capability is "
                   "still inferred for any user-facing project type.",
    },
    "Environments": {
        "classification": "safely_inferable",
        "default": "Local development only; no staging/production "
                   "environment is configured or assumed.",
    },
    "Testing Expectations": {
        "classification": "safely_inferable",
        "default": "Standard automated tests plus the platform's "
                   "deterministic checks; no additional expectation "
                   "stated.",
    },
    "Deployment": {
        "classification": "safely_inferable",
        "default": "Not authorised; matches the frontmatter default "
                   "deployment_authorised: false.",
    },
    "Constraints": {
        "classification": "optional",
        "default": "No additional constraints stated.",
    },
    "Acceptance Criteria": {
        "classification": "important_but_non_blocking",
        "default": "Not stated separately; derived from Functional "
                   "Requirements -- \"each stated requirement works as "
                   "described\".",
    },
    "Autonomous Permissions": {
        "classification": "safely_inferable",
        "default": "The platform's existing conservative defaults "
                   "(see .agentic/contract.md MAY ACT ALONE) -- never "
                   "widened by an absent section.",
    },
    "Protected Actions": {
        "classification": "safely_inferable",
        "default": "The platform's existing MUST QUEUE list (see "
                   ".agentic/contract.md) -- never narrowed by an absent "
                   "section.",
    },
}

REQUIRED_SECTION_ORDER = list(SECTION_SPECS.keys())

MATERIALLY_BLOCKING_SECTIONS = [
    name for name, spec in SECTION_SPECS.items()
    if spec["classification"] == "materially_blocking"
]

# Frontmatter: field -> (default, allowed values or None for free-form)
FRONTMATTER_DEFAULTS = {
    "agentic_project_version": (SCHEMA_VERSION, None),
    "project_id": (None, None),
    "name": (None, None),
    "project_type": ("web_application", None),
    "priority": (50, None),
    "autonomy": ("completion_only",
                ("completion_only", "milestone_review", "cycle_review")),
    "deployment_authorised": (False, (True, False)),
    "remote_push_authorised": (False, (True, False)),
}
