# ADR 0001 — OS-owned deterministic Context Broker

Status: accepted · Date: 2026-07-17

## Context

Prompts are currently assembled ad hoc (`load_prompt` concatenation,
`_snapshot` file inlining, JSON-dumped input data). Adding a code-context
engine, persistent memory, an Obsidian vault, and agent skills would create
four independent context injectors competing for the same window with no
budget, dedupe, or provenance — and each one a separate prompt-injection
surface.

## Decision

A single deterministic Context Broker (`core/context/`) is the only
component allowed to assemble model input. It is plain code, not an LLM
agent. All sources (code intelligence, memory, vault, skills, validation
failures) are passed to it as candidate items; it ranks (policy > relevance >
freshness > authority > dependency), deduplicates, enforces a hard token
budget with a protected output reserve, refuses to truncate mandatory
sections, records provenance, and persists a package ledger.

## Consequences

- Every model invocation in `project.py` / `orchestrator.py` migrates to
  broker-built packages (Phase 1 exit criterion).
- Retrieved repository text, memories, and skill instructions carry
  `trust_level: untrusted` and can never displace policy or output schemas.
- Token budgeting is centrally testable; savings are measurable per package.
- One extra layer of indirection at every call site — accepted cost.
