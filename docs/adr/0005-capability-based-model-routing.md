# ADR 0005 — Capability-based model routing

Status: accepted · Date: 2026-07-17

## Context

Routing today is name-based: `routing.primary` + ordered fallbacks, with
optional per-agent overrides. Different machines have different CLIs,
local models, and API keys; hard-coding provider names makes configurations
non-portable and lets unsuitable models (e.g. embedding models) be selected.

## Decision

Add `routing.mode: capability` alongside the preserved `simple` and
`per_agent` modes. Roles declare required capabilities (reasoning, coding,
review, long_running…); discovery reports per backend: installed/auth/smoke
state, models, structured-output support, context window when reliably
known, historical success rate, breaker state. A deterministic router picks
the best available backend, records a routing-decision explanation, and
enforces policies: reviewer independent from worker, no fallback on auth
failure or refusal, rebuild (never reuse) context for a smaller fallback
model, never select embedding models for generative roles.

## Consequences

- Configurations become machine-portable; `run setup` output feeds discovery.
- Existing simple/per_agent configs keep working unchanged.
- Routing decisions become auditable artefacts (`routing-decisions.jsonl`).
