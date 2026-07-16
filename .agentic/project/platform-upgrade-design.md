# Platform Upgrade Design — Context-Efficient Autonomous Development OS

Status: approved baseline for phases 1–11
Created: 2026-07-17
Baseline commit: 4381ad9 (dashboard) on 140ecc8 (hybrid build system)

## 1. Existing architecture (verified against code)

Entry point `.agentic/run` (argparse, no external CLI framework) dispatches to
`.agentic/core/*`. Two workflows exist:

1. **Maintenance tick** (`orchestrator.run_tick`): triage → conductor →
   worker → independent verifier → deterministic gate → trust policy →
   draft/queue. Uses `core.invoke.invoke_model` (role→provider table in
   `roles:`/`providers:` config) — the *API-provider* path.
2. **Full-project build** (`project.py`): plan → architect → persistent
   backlog (`projstate`, atomic YAML under `.agentic/project/`) → per-cycle:
   capacity gate (`capacity.decide_start`) → conductor → coder in persistent
   git worktree (`agentic/project` branch) → deterministic gate
   (`gate.run_checks`) with ≤3 repair attempts → QA reviewer → conditional
   security reviewer → cycle commit → cooling (`scheduler`, persisted
   `next_run_at`, never sleeps) → final audit (`final_audit`). Uses
   `backends.invoke_backend` — the *hybrid* path (cli/local/api backends,
   circuit breakers in `breaker.BreakerBoard`, capacity ledger TSVs).

Verified properties:

- Command execution single choke point: `execpolicy.py`. Model-originated
  commands never get a shell and must match `execution.safe_commands`
  verbatim. CLI adapter commands validated against `FORBIDDEN_COMMAND_TOKENS`.
- Path confinement: `gitops.safe_join` + `check_paths` + protected-path
  patterns; policy gate in code (`orchestrator.apply_policy`).
- Secrets: `redact.py` masks env values and token shapes in logs/prompts;
  `looks_like_secret` blocks diffs/orders embedding secrets.
- Configuration layering (config.py): config.yaml → config.machine.yaml
  (git-ignored) → profiles/NAME.yaml → AGENTIC_* env → CLI flags.
- Structured handoffs: JSON schemas in `.agentic/schemas/` validated locally
  (`schema.py`, no jsonschema dependency), one repair retry, then fallback.
- Fallback policy: never on auth/policy/budget/refusal (errors.NO_FALLBACK_KINDS).
- Capacity: local estimates only, labelled reported/estimated/unknown;
  self-imposed limits in `limits:`; no invented provider quotas.
- Scheduler: JSON state file, cooling clamps, operating window, resumable.
- Dashboard: loopback-only FastAPI (`.agentic/ui/`) + React (`ui/`),
  CSRF/Host checks, no arbitrary command endpoint, SSE events.
- Tests: 142 collected; mocked Transport/Runner/Caller fixtures; only
  `test_live_smoke.py` (1, skipped) can touch a real provider, opt-in.

**Prompt construction today** (the gap this upgrade closes): prompts are
assembled ad hoc at call sites — `load_prompt()` concatenates shared prompt
files; `_snapshot()` inlines up to 20 files/200 KB; input_data is JSON-dumped
by `cli_base.compose_prompt` or provider adapters. There is no token
budgeting, deduplication, provenance, retrieval, memory, or skills layer.

## 2. Target architecture

One new deterministic, OS-owned **Context Broker** becomes the only
component that assembles model input. Everything else feeds it:

```
                    ┌────────────────────────────────────────────┐
 plan ─▶ Architect  │                ORCHESTRATOR                │
                    │  (project.py cycle loop, unchanged shape)  │
                    └───────┬────────────────────────────────────┘
                            │ ContextRequest(role, task, budgets…)
                            ▼
                    ┌────────────────┐   sources (never inject on their own)
                    │ CONTEXT BROKER │◀── CodeIntelligenceAdapter (none/native/cce)
                    │ deterministic, │◀── MemoryService (progressive disclosure)
                    │ not an LLM     │◀── KnowledgeVault (sections only)
                    └───────┬────────┘◀── SkillRegistry (approved, pinned)
                            │ ContextPackage (budgeted, deduped, provenance)
                            ▼
                    ┌────────────────┐
                    │ CapabilityRouter│─▶ Backend Registry (cli/local/api)
                    └───────┬────────┘   + prompt caching in API adapters only
                            ▼
        workers: coder / ui_designer / qa_reviewer / security_reviewer
                            │
              Deterministic Gate → repair packets → Final Auditor
```

## 3. File/module plan

New code lives in `.agentic/core/` subpackages; existing modules keep their
paths and public functions.

| Component | New files | Touches existing |
|---|---|---|
| P1 Context Broker | `core/context/{__init__,broker.py,items.py,tokenizer.py,ledger.py}` | `project.py`, `orchestrator.py` (call sites), `backends.py` (accepts packages), `config.yaml` (`context:`) |
| P2 Code intelligence | `core/codeintel/{__init__,base.py,none_adapter.py,native.py,cce.py}` | broker code section; `run` (`context …` cmds); `doctor.py` |
| P3 Memory | `core/memsvc.py` (sqlite3, `.agentic/memory/memory.db`) | broker memory section; `run` (`memory …`); project.py write hooks |
| P4 Knowledge vault | `core/knowledge.py`, `.agentic/knowledge/` | project.py/final_audit writers; `run` (`knowledge …`); `.gitignore` |
| P5 Skills | `core/skillreg.py`, `.agentic/skills/registry.yaml` + `installed/` | broker skills section; `run` (`skills …`) |
| P6 Routing | `core/routing.py` (capability router + discovery record) | `backends.routing_chain` delegates when `routing.mode: capability`; `doctor.py`; `setupwiz.py` |
| P7 Council | `core/council.py` (repair packets, fingerprints, escalation) + schema updates | `project.py` review loop; `schemas/{work-order,worker,verification}.schema.json` extensions |
| P8 Caching | `providers/anthropic.py`, `providers/openai.py`, `providers/openai_compatible.py` (cache params + usage readback); telemetry columns in `capacity.py` | broker stable-prefix ordering already provides the prefix |
| P9 Capacity | extend `capacity.py` (envelope incl. review/repair reserve), `scheduler.py` (dynamic cooling inputs) | config `scheduler.capacity` |
| P10 Dashboard | new pages in `ui/src/pages/`, snapshot providers in `.agentic/ui/snapshots.py` | reuse existing service/security layer |
| P11 E2E | `tests/test_e2e_autonomy.py` | none (test-only) |

Roles are renamed only additively: existing role names (triage, conductor,
worker/coder, qa, security, verifier, architect) keep working; new roles
(ui_designer, final_auditor, memory_summarizer) are optional entries.

## 4. Data migrations

- `config version: 1 → 2`. Loader accepts both; `core/migrate.py` fills new
  sections (`context`, `memory`, `knowledge`, `skills`, `caching`, extended
  `routing`) with safe defaults when absent, so version-1 files keep working
  unchanged. Doctor reports the effective version.
- New runtime stores (all git-ignored): `memory/memory.db`,
  `memory/context-ledger.jsonl`, `memory/routing-decisions.jsonl`,
  `memory/code-index/` (adapter-owned), `knowledge/.obsidian/` (user's).
- No existing file changes format. `scheduler.json`, TSVs, backlog YAML are
  extended only with optional keys.

## 5. Configuration changes (all optional, defaulted)

`context:` (budgets/allocation/overflow per spec, per-role overrides),
`context.code_intelligence:` (provider none|native|cce, fallback, excludes),
`memory:` (enabled, retention, redaction), `knowledge:` (enabled, path),
`skills:` (auto_install:false, allow_scripts:false), `routing:` gains
`mode: capability` + `agents:` + `policies:` (simple/per_agent preserved),
`caching:` (anthropic ttl, enabled per backend type), `scheduler.capacity:`
(review reserve). An annotated example ships as
`.agentic/examples/config.example.yaml`.

## 6. Test strategy

- Every phase adds a focused test module `tests/test_<component>.py` using
  existing fixtures (Transport/FakeRunner/FakeCaller/Clock/sandbox).
- No real provider calls anywhere except the pre-existing opt-in live smoke.
- Security invariants get repository-wide tests (`tests/test_invariants.py`,
  grown per phase): no shell for model commands, no path escape, no
  credential persistence, no push/merge/deploy strings in autonomous paths.
- Full suite must pass before a phase is declared complete; phase commits
  are local only.
- Frontend: vitest component tests (`ui/src/test/`), run in P10.

## 7. Security boundaries

Unchanged and enforced: execpolicy shell rules; safe_join path confinement;
protected paths; redact-before-persist; loopback-only dashboard.
New boundaries added by this upgrade:

- Broker marks repository/skill/memory content `trust_level: untrusted`;
  untrusted items can never occupy or displace policy/schema sections.
- CCE adapter: structured invocation (argv, no shell), workspace-scoped
  paths, timeout, malformed-output handling, secret-pattern exclusion before
  indexing.
- Memory: redaction at write time; `sensitive` flag; no transcripts.
- Skills: pinned revision + checksum verification, disabled by default,
  scripts run only through execpolicy when separately approved.
- Routing: auth failure and refusal never trigger fallback (already coded;
  preserved in capability router).

## 8. Rollback strategy

Each phase is one local commit; `git revert <phase-commit>` restores the
previous behaviour. Every new subsystem has an `enabled:` switch defaulting
to a compatible mode (context broker falls back to legacy prompt assembly
only until P1 completes, after which it is the mandatory path but its
*sources* degrade individually: code intelligence → `none`, memory →
disabled, skills → disabled). Machine config is never touched by phases.

## 9. Known unknowns

- CCE (github.com/rajkumarsakthivel/code-context-engine): licence, CLI
  surface, and output format unverified — adapter is written against a
  mocked contract; live integration is opt-in and detection-gated.
- Real subscription CLI capacity signals vary by version; parsing remains
  best-effort and confidence-labelled.
- Anthropic/OpenAI cache field names drift; adapters send cache parameters
  only for explicitly configured API backends and read usage defensively.
- Windows Task Scheduler integration for cooling wake-ups stays a documented
  manual step (no service installation by the OS itself).
- skills.sh manifest format may differ from our manifest; `skills add`
  normalises and pins, never trusts remote metadata.

## 10. Phase test boundaries

| Phase | Boundary (focused tests must prove) |
|---|---|
| 1 | budget ceiling, output reserve, mandatory sections, dedupe, injection stays untrusted, every invocation goes through broker |
| 2 | fallback when CCE missing, stale index, exclusions, malformed output, token-bounded retrieval, Windows paths |
| 3 | isolation, redaction, progressive disclosure, supersession, expiry, corrupt-DB recovery |
| 4 | markdown validity, stable rewrites, user-section preservation, no `.obsidian` |
| 5 | unpinned/checksum-mismatch rejection, disabled skills never load, role scoping, budget applies |
| 6 | discovery, auth-failure no-fallback, breaker, reviewer independence, rebuilt fallback context, embedding-model exclusion |
| 7 | happy path, deterministic failure, reviewer repair rounds bounded, identical-failure fingerprints, escalation |
| 8 | prefix ordering, API-only cache fields, honest CLI labelling, estimate vs reported metrics |
| 9 | envelope refusal, retry-after, restart mid-cooling, operating hours |
| 10 | API contracts, component tests, degraded states, localhost security |
| 11 | full mocked scenario + failure-mode matrix + final evidence report |
