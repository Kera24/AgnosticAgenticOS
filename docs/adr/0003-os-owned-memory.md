# ADR 0003 — OS-owned persistent memory

Status: accepted · Date: 2026-07-17

## Context

Durable decisions, failures, and reviewer findings are currently scattered
across YAML state, TSV ledgers, and run artefacts. Claude-Mem demonstrates a
useful progressive-disclosure model (index → timeline → details) but is an
external, provider-adjacent tool with its own automatic injection hooks.

## Decision

Memory is an OS-owned service (`core/memsvc.py`, SQLite in
`.agentic/memory/memory.db`) with progressive disclosure and typed records
(requirement, constraint, decisions, failed_attempt, findings, outcomes…).
It never injects context itself — the Context Broker queries it. External
memory tools (claude-mem or others) may later be wired as optional adapters,
but the default store and the authority rules are ours:

- current project plan > historical memory,
- reviewer-verified > unverified,
- superseded records are never injected,
- redaction at write time; no credentials, no transcripts by default.

## Consequences

- Memory works for every backend (CLI, local, API) identically.
- Deterministic writes; model summarisation, when used, keeps source refs.
- One more runtime store to migrate/back up — mitigated by export command
  and corrupt-database recovery tests.
