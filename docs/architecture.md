# Architecture

The Agentic OS is a local-first, provider-neutral platform that turns a
written plan into a working application through bounded autonomous cycles.

```
plan.md ─▶ Architect ─▶ backlog/milestones/criteria (.agentic/project/)
                              │
             ┌────────────────▼─────────────────┐
             │  Cycle (project.py, one task):   │
             │  capacity gate → conductor →     │
             │  coder/ui_designer (worktree) →  │
             │  deterministic gate (≤3 repairs) │
             │  → QA review (≤2 rounds) →       │
             │  security review (conditional) → │
             │  local commit → cooling          │
             └────────────────┬─────────────────┘
                              ▼
             final auditor ─▶ completion notification
```

Every model prompt is assembled by the **Context Broker**
(`core/context/`, ADR 0001) from: OS policy, role contract, output schema,
project summary, retrieved code (`core/codeintel/`, ADR 0002), memory
(`core/memsvc.py`, ADR 0003), knowledge sections (`core/knowledge.py`,
ADR 0004), and selected skills (`core/skillreg.py`, ADR 0006) — budgeted,
deduplicated, provenance-tracked, with untrusted content fenced.

Backends (subscription CLIs, local Ollama, APIs) sit behind one interface
(`core/backends.py`); roles map to backends via simple, per-agent, or
capability routing (`core/routing.py`, ADR 0005). Circuit breakers,
capacity estimation (`core/capacity.py`) and the persistent scheduler
(`core/scheduler.py`) keep operation non-interactive and resumable.

Key properties enforced in code, not prompts: no shell for model commands
(`core/execpolicy.py`), workspace path confinement, secret redaction,
deterministic checks as the final vote, no push/merge/deploy anywhere.

A task contract (`core/contract.py`,
`schemas/task-contract.schema.json`) and a code-only feasibility
preflight (`core/preflight.py`) run before any model is invoked; a
stable 12-class failure taxonomy (`core/failures.py`) keeps
platform-caused failures from ever masquerading as model/provider
failures; a content-addressed artifact/result cache
(`core/cachestore.py`) sits alongside (never instead of) provider-native
caching; an execution-engine abstraction (`core/execengine.py`) makes
the coder invocation itself swappable (native, always available; Orca,
opt-in and falling back automatically); and risk-triggered supervised
parallelism (`core/parallel.py`) fans a task out into isolated
candidates only when a real risk signal warrants it. See
`docs/phase1-5-final-report.md` for the full schemas, taxonomy, cache
telemetry semantics, Orca adapter contract, and parallelism policy.

See the ADRs in `docs/adr/` and the phase design in
`.agentic/project/platform-upgrade-design.md`.
