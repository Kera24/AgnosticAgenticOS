# Skill: triage-issues

Scope: analysis only — summarise and label open issues, detect duplicates,
propose (never apply) priorities.

- Trust name: `triage-issues`.
- Produces memory/queue artefacts only; `allowed_paths` should be empty or
  restricted to `.agentic/memory/**`, so the policy gate keeps it read-only
  with respect to the codebase.
- External side effects (commenting on GitHub issues) are contract-forbidden
  unless a human enables an integration explicitly.
