# Memory

OS-owned persistent memory (`.agentic/core/memsvc.py`, SQLite at
`.agentic/memory/memory.db`) with progressive disclosure:

1. `memory search "<query>"` — compact rows (id, type, title, summary)
2. `memory timeline <id>` — surrounding project events
3. `memory show <id...>` — full records for chosen ids only

Typed records (requirement, constraint, decisions, failed_attempt, bug,
resolution, reviewer/security findings, cycle/milestone outcomes, project
summary) are written **deterministically** by cycle hooks — outcomes and
durable lessons, never transcripts.

## Rules

- Everything is redacted at write time; credentials never enter the store.
- Records are project-isolated; superseded and expired records are never
  injected; `sensitive` records are excluded from injection entirely.
- The current project plan outranks memory (memory is optional broker
  content; the work order is mandatory). Reviewer-verified records rank
  above unverified ones.
- Injection is bounded by `memory.inject_limit` (default 8 compact
  summaries) — never the full history.
- A corrupt database is moved aside (`memory.db.corrupt-<ts>`) and
  recreated; WAL mode covers interrupted writes.

```powershell
py .agentic/run memory status
py .agentic/run memory search "parser"
py .agentic/run memory forget <id>     # explicit, permanent
py .agentic/run memory compact
py .agentic/run memory export backup.json
```

An external claude-mem adapter can be added later; the OS-owned store
stays the default and never competes with another auto-injector.
