# ADR 0002 — Pluggable code-intelligence adapter

Status: accepted · Date: 2026-07-17

## Context

The coder currently sees a bounded raw snapshot (≤20 files / 200 KB matching
allowed paths). Retrieval-quality context needs indexing and search. The
candidate engine (Code Context Engine, CCE) is an external project whose
licence, stability, and availability on any given machine are unverified.

## Decision

Define an internal `CodeIntelligenceAdapter` interface (initialize/status/
index_full/index_changes/search/expand/related/remove_project/health_check)
owned by this repository. Three implementations:

- `none` — preserves today's snapshot behaviour (default fallback),
- `native` — repository search (git grep/ls-files) with token bounds,
- `cce` — detects an installed CCE, validates version, invokes it as an
  argv subprocess (never a shell), enforces workspace paths and timeouts.

The broker consumes the interface only; CCE is never imported or vendored.

## Consequences

- The OS works identically (less context quality) when CCE is absent;
  doctor reports which adapter is active and why.
- CCE upgrades/breakage are isolated behind version validation and
  malformed-output handling; CI mocks the adapter and never requires CCE.
- Secrets and configured exclusions are filtered before indexing.
