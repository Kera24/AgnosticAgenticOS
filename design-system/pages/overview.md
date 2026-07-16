# Page: Overview (overrides MASTER)

The overview is the cockpit's main instrument panel. Its job: answer "what is
the system doing right now, and does it need me?" in under five seconds.

## Layout

```
┌ status strip (project · state chip · cooling countdown · next run) ┐
├────────────────────────────────────────────────────────────────────┤
│  ORCHESTRATION RAIL  (signature element, full width)               │
│  [Architect]──[Conductor]──[Coder]──[GATE]──[QA]──[Security]──[⑂] │
├──────────────────────────┬─────────────────────────────────────────┤
│ Requirement/milestone/   │  Attention: blockers, capacity decision, │
│ task completion meters   │  backend availability, audit status      │
└──────────────────────────┴─────────────────────────────────────────┘
```

## Rail rules

- Stage states: `done` (ok colour, filled node), `active` (accent, pulsing
  dot; static under reduced motion), `pending` (hairline outline), `skipped`
  (dashed outline + "skipped" label, e.g. conditional security), `failed`
  (fail colour), `blocked` (block colour).
- The GATE node is square with a hatched border and mono label — visually
  non-AI. Commit node is the branch glyph.
- Under each AI node: backend chip (mono, e.g. `claude`) showing which
  backend served that stage; a reroute shows `codex→ollama`.
- Selecting a node opens the evidence drawer: timings, backend, verdicts,
  log excerpt for that stage of the latest cycle.
- Elapsed time for the active stage ticks in mono; `aria-live="off"`.

## Meters

Completion meters are hairline horizontal bars (no radial gauges): label,
n/total in mono, bar. Test/build health is a status chip row, not a chart.

## Empty state (no project)

Show the rail dimmed with all nodes pending plus a single instruction block:
"No project started — create one in Projects" with a button. No illustration.
