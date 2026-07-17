# Capacity Estimation & Scheduling

The OS never invents provider quotas. Every figure is labelled
`reported` (a provider said so), `estimated` (local history/limits), or
`unknown`.

## The cycle envelope

Before starting a task (`core/capacity.py`):

```
envelope = (conductor + coder + QA [+ security] + repair reserve
            + orchestration overhead) × size factor
required = max(envelope, highest similar historical cycle) × safety(1.35)
```

Policy knobs (`scheduler.capacity`):

- `include_review_reserve: true` — QA/security/repair tokens are part of
  the envelope; a task that fits only without its review does not start.
- `stop_before_exhaustion: true` — refuse when reported/limit-derived
  capacity falls short (disable to proceed with a recorded warning).
- `confidence_required: false` — set true to never start on unknown
  capacity.

Deferred starts persist why (`scheduler.json → deferred`: reason,
estimated requirement, confidence, next eligible time).

## Cooling

After every cycle: success/failure → 30 min defaults; rate/usage limits →
explicit retry-after, else breaker history; consecutive failures double
the failure cooldown (`scheduler.cooling.dynamic`, capped ×8); all clamped
to [minimum, maximum]. Long waits are **persisted** (`next_run_at`) —
`project-run` exits and any timer re-invokes it. The whole cycle envelope
must also fit the remaining operating window.

```powershell
py .agentic/run capacity            # ledger + next-cycle estimate
py .agentic/run project-run --now   # manual override: clear cooling
```

Windows Task Scheduler can re-invoke `py .agentic/run project-run`
periodically; the scheduler state makes re-invocation idempotent.
