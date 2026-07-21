# Capability Intelligence Upgrade — Gap Analysis & Design (Phase 0)

Date: 2026-07-20 · Baseline commit: `d373a3f` (dirty: uncommitted
model-resolution fix, see below) · Baseline tests: 492 collected, all
passed, 1 skipped (opt-in live smoke), 0 failed, exit 0 · doctor: not
re-run live in Phase 0 (would probe real `codex`/`ollama` binaries on
this machine; import-level sanity check only, per "no live provider
calls during automated validation").

Uncommitted at Phase-0 start (my own prior-session work in this
conversation, preserved, not reverted): central model-resolution policy
(`core/modelres.py`), Codex/Claude/Ollama/API adapters wired to it,
`doctor.py` role→backend→model resolution, `setupwiz.py` model-neutral
CLI config. These are treated as part of the existing baseline for this
upgrade — they are exactly the "Model Capability Registry" precursor
Phase 8 builds on.

This is the **third** phased upgrade to this codebase. The first
(`platform-upgrade-design.md`, Context Broker/memory/knowledge/skills/
routing/caching) and second (`multi-project-gap-analysis.md`, registry/
fleet/worktrees/dashboard) both shipped fully, phase-gated, tests-green
at every step. This document follows the same discipline for capability
intelligence.

## 1. What already exists (verified, reused as-is)

| Area | Where | State |
|---|---|---|
| Backend adapters (CLI/local/API), model resolution | `core/backends.py`, `providers/*`, `core/modelres.py` | Complete; central resolver just landed this session |
| Routing (simple/per_agent/capability), embedding exclusion, Qwen-unverified exclusion | `core/routing.py` | Complete for backend-type capability levels; no per-model class |
| Capacity / circuit breaker | `core/capacity.py`, `core/breaker.py` | Complete; backend-keyed, no model-class awareness |
| Fleet scheduler (multi-project slots, cooling, TTL reaping) | `core/fleet.py`, `core/scheduler.py` | Complete; pools keyed by backend name/type, no frontier-class pool |
| Context Broker (single funnel, budgets, dedup, provenance, trust levels) | `core/context/{broker,items,compose}.py` | Complete as the sole assembler; fixed category set, role/budget driven only, no capability-plan input type |
| Memory | `core/memsvc.py` | Complete, flat SQLite (`records` table), keyword search, progressive disclosure — no graph/edges |
| Knowledge vault | `core/knowledge.py` | Complete generator + keyword-scored read-back into context; not read by planning |
| Skills registry (checksum pin, static script/hook scan, risk levels, enable/disable) | `core/skillreg.py` | Complete and real (SHA-256, `SUSPICIOUS_RE` scan) — not a stub |
| Skill marketplace (quarantine, prompt-injection scan, evaluate, approve, rollback) | `core/skillmarket.py` | Complete lifecycle, all local/metadata-only (`skillreg.py:16-20` — no network fetch by design) |
| MCP gateway (registration, tool allowlist, destructive-tool gating, project scope, health check, task-scoped `.mcp.json`) | `core/mcp.py` | Complete for local/manual configuration; no discovery/quarantine pipeline |
| Supabase adapter (environment policy: local automatic, staging/prod approval_required/denied) | `core/supabasex.py` | Complete — **this already is** the Phase 7 Supabase safety model |
| Docker adapter (project-scoped) | `core/dockerx.py` | Complete for running project-owned compose/services |
| Trust ledger (performance-earned tiers: watch/queue/auto) | `core/trust.py` | Complete — a *different, complementary* axis from acquisition-time risk (see §7) |
| Project registry, lifecycle, backend_profile overlay | `core/registry.py`, `core/projectops.py` | Complete |
| Architect → backlog → cycle → gate → QA/security → cooling → final audit | `core/project.py` | Complete as a flat-task pipeline; no capability concept |
| Deterministic gate, protected paths, secret redaction, no-shell execpolicy | `core/gate.py`, `core/gitops.py`, `core/redact.py`, `core/execpolicy.py` | Complete, hard-enforced |
| Config layering + versioned migration (v1→v2, additive, non-destructive) | `core/config.py`, `core/migrate.py` | Complete pattern to extend (v2→v3) |
| Doctor | `core/doctor.py` | Complete flat checklist (just extended with per-role model resolution); no readiness tiers |
| Dashboard backend + pages | `.agentic/ui/`, `ui/src/pages/` | 15 pages exist (Portfolio, Routing, Skills, Mcp, Context, Memory, Knowledge, …); `/skills/market` and `/auth` endpoints exist server-side with **zero** frontend consumers today |

## 2. What is missing (verified absent by repository-wide search, not inferred)

| Component | Evidence it's absent |
|---|---|
| Structured/frontmatter `project.md` parser, assumption ledger, blocking-question classifier | `project_start` reads the plan as an opaque string (`project.py:143-144`); zero hits for "frontmatter" under `core/` |
| `project template` / `project validate` / `project assumptions` / `project specification` commands | `.agentic/run` has no such subcommands |
| Capability taxonomy (declarative data) | No file, no schema, zero source hits for "capability taxonomy" |
| Capability Plan / Requirements Intelligence Engine | `architect.schema.json` has one item shape (`backlog[]`); `requirements_map` is written once (`project.py:170`) and never read again |
| Capability Graph (persistent, nodes/edges, satisfaction states) | Memory is a flat table with no edge/relation concept; no graph module anywhere |
| Capability Resolver (installed → registries → rank → acquire) | No network fetch exists anywhere in skills/MCP code by design |
| Plugin concept (skill+hook+MCP+executable bundle) | Zero hits for "plugin" anywhere under `.agentic/` |
| Live external registry search for skills | `skillmarket.discover()` reads a pre-mirrored local index only |
| Deterministic auto-approval **policy** wired to anything | `skills.auto_install` config key exists but zero code reads it — inert |
| MCP discovery/quarantine/trust pipeline analogous to skills | `mcp.py`'s `environment` field and `"environment"` scope value are stored but never enforced (`call()` only checks `machine`/`project`) |
| Model Capability Registry (frontier/high/medium/lightweight classes, dynamic, cross-backend) | `routing.py` only has backend-*type* default capability dicts (cli/api/local/custom_command); no per-model class, no named tiers |
| Reserved frontier capacity in the scheduler | `fleet.DEFAULT_CONCURRENCY` pools by backend name/type only |
| Hierarchical orchestrator / Frontier Orchestrator role with escalation | No escalation hierarchy beyond primary/fallback chain exists |
| Canonical role registry (single source of truth) | Role sets are duplicated independently in `routing.py` (`ROLE_ALIASES`), `setupwiz.py` (hardcoded tuple), and inline literals in `project.py` |
| Completion Contract (per-criterion evidence traceability) | `final_audit` is a fixed checklist (`project.py:827-916`); `requirements_map` write-once, never read back; no commit/test/reviewer linkage per requirement |
| Dashboard: capability plan, skill discovery, MCP/plugins, model orchestration, completion contract, autonomy inbox views | No frontend page references these concepts; `human_blockers` is the closest analogue to an inbox, per-project only |
| Doctor readiness tiers (platform/project/pending/blocked) | One flat `(level, message)` list; the READY/NOT-READY line is one more flat entry |

## 3. A confirmed policy conflict that needs your decision before Phase 1

This is the one finding from Phase 0 that materially changes architecture,
so I'm stopping on it rather than deciding it myself (self-broadening my
own permissions is explicitly forbidden).

`guardrails/protected-paths.txt` lists `**/migrations/**`, `**/migrate/**`,
`Dockerfile`, and `docker-compose*.yml` as protected. `project.py:434-439`
**hard-fails** (not "queue for approval" — the cycle fails outright) any
work order whose `allowed_paths` would grant a protected pattern. As the
repository stands today, a coder task can **never** be given permission to
create a Supabase migration file or a Dockerfile — even though
`core/supabasex.py` already implements exactly the safety model this
upgrade asks for (local: automatic; staging/production: approval_required/
denied) and `core/dockerx.py` already runs project-scoped Docker services.

The blanket protected-paths entries predate that adapter work and are
stricter than the adapters they'd now be blocking. Reconciling this needs
one of:

1. **(recommended)** Narrow `protected-paths.txt`: drop `**/migrations/**`,
   `**/migrate/**`, `Dockerfile`, `docker-compose*.yml` from the hard-block
   list (file *creation/editing* becomes ordinary reviewed coder work,
   exactly like any other source file); keep the actual safety boundary
   where it already correctly lives — `supabasex.environment_policy()`
   (apply/reset/seed gated by environment) and `dockerx`'s project-scope
   enforcement. Add a new "migration and compose files always require the
   QA+security reviewer pass, never `execution.mode: auto` without review"
   rule in the Capability Plan (belt-and-suspenders, enforced by code, not
   by blanket path denial).
2. Keep the blanket block and instead special-case exactly two path globs
   (project's own `supabase/migrations/**` and `docker-compose.yml`/
   `Dockerfile` at the project root) as capability-plan-authorised
   exceptions, still denying everything else the pattern set protects
   (`.env*`, `**/secrets/**`, `**/auth/**`, `.github/workflows/**`, etc.).

Option 2 is safer-by-default and touches less shared policy surface, at
the cost of the resolver needing to special-case two globs instead of the
general contract description in `AGENTS.md`/`contract.md` staying exactly
true. **I recommend option 2** unless you'd rather simplify the contract
text itself. This is exactly the kind of call Phase 6/7 code will be built
around, so I'm asking now rather than after the fact.

**Decision recorded (2026-07-20): option 1** (narrow the two globs via
project-scoped, Capability-Plan-authorised exceptions for the project's
own `supabase/migrations/**` and root `Dockerfile`/`docker-compose.yml`;
every other protected pattern stays hard-blocked exactly as today).
Implementation lands in **Phase 7** (MCP/Supabase/Docker capability
resolution), the first phase that actually has a Capability Plan able to
authorise the exception — no `guardrails/protected-paths.txt` or
`project.py` change is needed before then, so Phase 1 does not touch
this file.

## 4. Proposed modules (new code, additive)

| # | Component | New files | Touches existing |
|---|---|---|---|
| P1 | Project Specification Parser | `core/projectspec/{__init__,parser.py,schema.py,assumptions.py}` | `run` (`project template/validate/assumptions/specification`), `projectops.py` |
| P2 | Capability Taxonomy | `core/capability/taxonomy.py`, `.agentic/capabilities/taxonomy.yaml` (+ `org/` override dir) | `schema.py` (validation) |
| P3 | Requirements Intelligence Engine | `core/capability/requirements.py` | `run` (`capability analyse/plan/explain/refresh`), consumes P1+P2 |
| P4 | Capability Graph | `core/capability/graph.py`, persisted `<project>/capability-graph.json` | `projstate.py` (paths), dashboard |
| P5 | Capability Resolver | `core/capability/resolver.py` | `run` (`capability resolve/status/candidates/retry`), `skillmarket.py`, `mcp.py` |
| P6 | Skill acquisition trust engine (extends, does not replace, `skillreg`/`skillmarket`) | `core/skillacquire.py` (LEVEL 0-4 deterministic policy, registry search adapter interface) | `skillmarket.py` (quarantine reuse), `run` (`skills policy/discover-for-project/evaluation/provenance/rollback`) |
| P7 | MCP/plugin resolver | `core/mcpresolve.py`, `core/pluginreg.py` | `mcp.py` (reuse `add`/`task_config`), `run` |
| P8 | Model Capability Registry | `core/modelcap.py` (extends `modelres.py` + `routing.discover`) | `routing.py` (class-aware chain), `doctor.py` |
| P9 | Hierarchical Orchestrator + Role Assignment | `core/rolereg.py` (canonical role registry, replaces the 3 duplicated lists), extends `routing.py` | `project.py` call sites (role names unchanged, additive escalation) |
| P10 | Capability-aware Context Broker integration | new `ContextItem` category `capability`, new `context/capability_items.py` | `compose.py` (one new `extra_items` source, funnel unchanged) |
| P11 | Completion Contract | `core/completion.py` | `project.py::final_audit` (extends, not replaces) |
| P12 | Dashboard views | `ui/src/pages/{CapabilityPlan,SkillDiscovery,McpPlugins,Orchestration,Completion,AutonomyInbox}.tsx` | `.agentic/ui/app.py` (new read-only snapshot endpoints), `snapshots.py` |
| P13 | Project template generator | `core/projectspec/template.py` | `run` (`project template`) |

No existing module is rewritten. `architect.schema.json`/`project.py`'s
cycle loop keep their current shape; the Capability Plan is consumed
*before* the architect call (enriching its input) and the Capability Graph
is updated *after* each cycle — both are additive hooks, not replacements.

## 5. Schemas

New JSON Schemas under `.agentic/schemas/`, validated the same way existing
ones are (`core/schema.py`, no external `jsonschema` dependency):

- `project-specification.schema.json` — normalised `ProjectSpecification`
- `capability-plan.schema.json` — `CapabilityPlan` + `CapabilityRequirement`
- `capability-graph.schema.json` — nodes/edges/satisfaction states
- `resolution-candidate.schema.json` — `ResolutionCandidate`
- `completion-contract.schema.json` — `CompletionContract`
- `work-order.schema.json` — **extended** (additive optional fields:
  `required_capabilities`, `selected_skills`, `selected_mcp_tools`,
  `selected_plugin_components`, `evidence_requirements`) — existing
  required fields unchanged, so v1 work orders still validate

## 6. State and configuration migrations

- Config version 2 → 3 in `core/migrate.py`, same additive pattern as
  1→2: new top-level sections (`capability_intelligence`,
  `capability_resolution`, `skills.discovery`/`skills.trust`,
  `mcp.resolution`, `orchestration`, `completion`) default-filled when
  absent; existing v2 files keep working unchanged; doctor reports the
  effective version and which sections were filled.
- New per-project state (git-ignored, alongside existing
  `backlog.yaml`/`progress.yaml`): `capability-plan.yaml`,
  `capability-graph.json`, `completion-contract.yaml`. All rebuildable
  deterministically from `project.md` + resolver state — never the sole
  source of truth for anything security-relevant.
- New machine-local cache (git-ignored): `.agentic/memory/registry-cache/`
  (mirrored external-registry metadata, TTL-refreshed, never contains
  credentials).
- `skills/registry.yaml` and `<home>/mcp.json` formats are extended with
  optional fields only (trust level, pinned revision provenance) —
  existing entries parse unchanged.

## 7. Security model

- **Acquisition-time risk tiers (new, this upgrade)** vs **performance
  trust (`trust.py`, existing)** are two different axes and stay separate:
  a skill can be LEVEL 2 (auto-approved low-risk at acquisition) and still
  be `watch` tier (unproven in this repo) until it earns runs — the
  existing trust ledger keeps deciding whether *output* is trusted enough
  to skip review; the new engine only decides whether the skill package
  itself was safe to *load* at all.
- Deterministic policy, not model discretion, decides LEVEL 2 auto-approval
  (mirrors the existing pattern: deterministic gate always outvotes a
  model). The orchestrator model may *recommend* a candidate; it cannot
  *approve* one.
- All new network fetch (registry search/download) goes through a single
  new choke point (`core/registryfetch.py`, reusing `providers.base`'s
  existing transport abstraction — same fixture-injectable pattern as
  every provider adapter) so automated tests never touch the network,
  matching `execpolicy.py`'s existing "one choke point" precedent for
  subprocess execution.
- Plugin components are decomposed and each sub-component (skill/hook/MCP/
  executable) goes through its *own* existing gate — a plugin never gets a
  blanket approval; an executable component inside an otherwise-safe
  plugin is quarantined independently, same as a bare skill would be.
- MCP resolution reuses `mcp.py`'s existing destructive-tool regex and
  tool-allowlist mechanism unchanged; new code only decides *which*
  server to configure and *how narrow* to make the allowlist, never
  loosens the existing enforcement.
- Frontier-orchestrator/model-registry work never touches credentials —
  it reads the same safe `--version`/`login status`/`ollama list`
  probes `modelres.py`/`authx.py` already use.
- §3's protected-paths decision is the only place this upgrade proposes
  narrowing an existing hard boundary, and it's called out for explicit
  sign-off rather than folded in silently.

## 8. Test plan

- Every new module gets a focused `tests/test_<module>.py` using the
  existing fixture set (`FakeRunner`, `Transport`, `Clock`, `sandbox`,
  the new `registryfetch` fixture) — no live network, no live provider
  calls, matching the existing `test_live_smoke.py`-is-the-only-opt-in-
  exception precedent.
- Six mocked end-to-end fixtures per Phase 13 of the spec, built the same
  way `test_multiproject_e2e.py` already demonstrates (`ScriptedAdapter`,
  monkeypatched `backends.build_backend`).
- `tests/test_invariants.py` grows: no network call in the default test
  run (assert no unmocked `registryfetch` transport is constructed), no
  plugin ever receives broader permission than its riskiest component,
  package/leak test extended to confirm the new registry cache and
  capability-graph files are excluded from `agentic package`.
- Full suite + doctor + package/leak test run after every phase; a phase
  is not reported done if any of those regress.

## 9. Backward compatibility

- Projects with no `project.md` frontmatter, no capability plan, and no
  completion contract keep working exactly as today — every new engine
  degrades to "nothing detected, proceed with existing behaviour" rather
  than requiring migration. `plan.md` (the current format) stays a valid
  input: it becomes a `ProjectSpecification` with every optional section
  empty and zero frontmatter, not a parse error.
- `routing.mode: simple` (today's default) is untouched; model-class-aware
  routing is additive under `routing.mode: capability` (already the
  documented advanced mode) plus the new `orchestration:` section.
- Existing skills/MCP servers keep working with their current trust
  state; the new LEVEL 0-4 tiering only applies going forward to newly
  *acquired* candidates — nothing already `enabled` gets silently
  re-evaluated or disabled.

## 10. Rollback plan

- Every phase is a separate local commit (never pushed), so any phase can
  be reverted independently without touching the phases before it — new
  modules are additive files plus small, isolated call-site hooks, not
  edits scattered through existing logic.
- `capability_intelligence.enabled: false` (config) fully disables Phases
  1-11's new behaviour at runtime, falling back to exactly today's
  `project_start`/cycle flow, without needing a code revert.
- State files are all derived/rebuildable (§6) — deleting
  `capability-plan.yaml`/`capability-graph.json`/`completion-contract.yaml`
  for a project and re-running `capability plan`/`capability resolve`
  reconstructs them from `project.md` + current resolver state.

## 11. Execution plan for the remaining phases

Phases 1-13 as specified are, done with the same rigor as the two prior
upgrades (real tests, real integration, no stubs presented as complete),
a multi-week engineering program — dozens of new modules, six schemas, a
taxonomy covering ~39 categories, six end-to-end fixtures, and 20 pages
of documentation. I'm not going to compress that into unverified
one-shot code; each phase will land the way P1-P13 above are scoped,
test-gated, with a Phase Completion Report per the spec's own format
before the next phase starts.

Two things I need from you before Phase 1 starts:

1. **§3's decision** (option 1 vs option 2 vs "keep it queued, don't
   change protected-paths at all and accept migrations/Docker files
   always route to human review").
2. Confirmation you want me to proceed phase-by-phase now (I'll continue
   autonomously through routine implementation choices per your original
   instruction and only stop again for another finding of this kind), as
   opposed to picking a narrower starting slice first.
