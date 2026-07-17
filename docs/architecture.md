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

See the ADRs in `docs/adr/` and the phase design in
`.agentic/project/platform-upgrade-design.md`.
