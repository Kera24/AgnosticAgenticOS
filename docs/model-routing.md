# Model Routing

Three modes (`routing.mode`), all preserved:

- `simple` — one primary + ordered fallbacks (set by `run setup`).
- `per_agent` — explicit chain per role.
- `capability` — roles declare required capabilities; the router picks
  from what this machine actually has (ADR 0005).

## Capability mode

Backends declare `capabilities:` (or inherit honest type defaults);
roles declare requirements and optional ordered `preferred:` entries in
`routing.agents`. See `.agentic/examples/config.example.yaml`.

Deterministic ordering: breaker health → preferred rank → capability
strength → 24 h success rate. Enforced exclusions:

- backends in `authentication_required` state — an auth failure is never
  routed around;
- embedding models for any generative role;
- local backends from fallbacks when `allow_local_fallback: false`.

`reviewer_different_from_worker: true` puts the worker's backend last in
reviewer chains; an unsatisfiable case is recorded, never silent.

Every chain computation appends an explanation (chain, candidates,
rejections with reasons, warnings) to
`.agentic/memory/routing-decisions.jsonl`:

```powershell
py .agentic/run backends decisions
py .agentic/run backends discover     # live probes: installed/auth/models
```

## Fallback rules (all modes, enforced in code)

Never on auth failure, refusal, policy or budget stops. Rate-limited
backends respect retry-after and breaker cooling. A fallback model gets a
**rebuilt** context package sized to its own window — never the primary's
prompt.
