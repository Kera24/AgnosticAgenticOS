# AGENTIC OS CONTRACT

The contract defines what the system may do alone, what must wait for a
human, and what must trigger an alert. Areas are configurable via
`config.yaml` (`contract.extra_protected_paths`, `trust.sensitive_skills`,
`trust.sensitive_auto_allowed`) and `guardrails/protected-paths.txt`.

## MAY ACT ALONE

- Analyse repository state (commits, status, issues, CI results).
- Create isolated worktrees and branches.
- Fix lint or narrowly scoped test debt within configured limits.
- Update internal Agentic OS memory (`.agentic/memory/*`).
- Produce draft changes in an isolated worktree.
- Run approved local verification commands.

## MUST QUEUE

Verified work in these areas always waits for human approval:

- Authentication or authorisation
- Payments or billing
- Database migrations
- Production or deployment configuration
- Secrets or credentials
- Dependency additions (lockfiles, manifests)
- Public API changes
- Destructive data operations
- Changes exceeding configured line/file limits
- Ambiguous requirements (conductor must set `action: queue`)
- Skills that have not earned the `auto` trust tier
- Push, merge, release, deployment, or any external communication

## MUST ALERT

Alerts are written to `.agentic/memory/STATE.md`, the run log, and stderr:

- Budget threshold exceeded (warning at `budget.warning_percentage`)
- Secret requested or detected in content
- Repeated verification failure (2+ consecutive for a skill)
- Standing goal violated
- Provider responses repeatedly malformed
- All configured providers unavailable
- Protected branch or protected path modification attempted
- Trust tier automatically demoted
