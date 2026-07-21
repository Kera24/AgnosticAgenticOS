"""`agentic project template` — a complete, commented project.md the user
can fill in without knowing anything about skills, MCP servers, agents,
or models. Every section here matches `schema.SECTION_SPECS` exactly, so
the generated template and the parser can never drift apart."""
from .schema import SECTION_SPECS

_SECTION_HINTS = {
    "Product Vision": "One or two sentences: what is this, and who is "
                      "it for?",
    "Problem": "What problem does this solve? Why does it need to exist?",
    "Target Users": "Who uses this? (e.g. \"small hotel owners\", "
                    "\"internal support staff\")",
    "User Roles": "If different people need different access, list the "
                  "roles here (e.g. guest, admin). Leave blank for a "
                  "single general user.",
    "Functional Requirements": "A bullet list of what the application "
                               "must actually let a user do. Be as "
                               "specific as you can -- this drives "
                               "everything else.",
    "User Journeys": "Optional: describe a few key end-to-end flows a "
                     "user takes through the product.",
    "Design Direction": "Optional: any visual style, brand, or reference "
                        "sites you want followed.",
    "Technical Preferences": "Optional: a specific language, framework, "
                             "or hosting provider you already use or "
                             "prefer. Leave blank to let the system "
                             "choose sensible defaults.",
    "Integrations": "Optional: third-party services this needs to talk "
                    "to (e.g. Supabase, Stripe, a mail provider).",
    "Data and Privacy": "Optional: what kind of data is stored, and any "
                        "privacy expectations. Do not put real "
                        "credentials or secrets anywhere in this file.",
    "Security Requirements": "Optional: anything beyond standard "
                             "practice you specifically require.",
    "SEO and Discoverability": "Optional: specific search terms, "
                               "locations, or ranking goals. A public "
                               "site gets baseline SEO regardless.",
    "Accessibility": "Optional: specific accessibility requirements "
                     "beyond the baseline that's always applied.",
    "Environments": "Optional: which environments this needs (local is "
                    "always included; list staging/production only if "
                    "you already have them).",
    "Testing Expectations": "Optional: anything beyond the standard "
                            "automated tests and checks that always run.",
    "Deployment": "Optional: where this should eventually be deployed. "
                 "Nothing is ever deployed automatically -- see "
                 "`deployment_authorised` above.",
    "Constraints": "Optional: anything the system must avoid or respect "
                   "(budget, timeline, existing systems it must not "
                   "touch).",
    "Acceptance Criteria": "Optional: how you'll know this is done. "
                           "Leave blank to derive it from Functional "
                           "Requirements.",
    "Autonomous Permissions": "Optional: leave blank to use the "
                              "platform's default safe boundaries "
                              "(see .agentic/contract.md).",
    "Protected Actions": "Optional: anything you want to explicitly "
                         "forbid, on top of the platform's existing "
                         "protections (no push, no deploy, no production "
                         "database changes, etc. are already forbidden "
                         "by default).",
}

_TEMPLATE_HEADER = """---
# agentic project.md -- fill in what you know, leave the rest blank.
# Nothing below requires any technical setup on your part -- the system
# figures out everything it needs automatically.
agentic_project_version: 1
project_id:                       # leave blank; assigned on registration
name: {name}
project_type: web_application     # e.g. web_application, api_service,
                                   # internal_tool, static_site, cli_tool
priority: 50                      # 1-100, higher runs first among your
                                   # registered projects
autonomy: completion_only         # completion_only | milestone_review |
                                   # cycle_review -- how often you want to
                                   # be asked to look at progress
deployment_authorised: false      # the system NEVER deploys unless this
                                   # is explicitly true, and even then
                                   # only to environments you configure
remote_push_authorised: false     # the system NEVER pushes to a remote
                                   # git repository unless this is true
---

"""


def render_template(name="My Project"):
    """A complete commented template. Every SECTION_SPECS entry appears,
    marked optional/recommended, with a plain-language hint -- never
    exposing internal capability/skill/model terminology."""
    parts = [_TEMPLATE_HEADER.format(name=name)]
    for section, spec in SECTION_SPECS.items():
        marker = ("required" if spec["classification"] == "materially_blocking"
                  else "recommended"
                  if spec["classification"] == "important_but_non_blocking"
                  else "optional")
        parts.append("## %s\n" % section)
        parts.append("<!-- %s -- %s -->\n\n"
                     % (marker, _SECTION_HINTS.get(section, "")))
    return "".join(parts)
