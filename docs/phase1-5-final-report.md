# Phase 1–5 Final Report: Reliability, Caching, Native Proof, Orca Adapter, Supervised Parallelism

This is the completion deliverable for the 5-phase upgrade ("reliability
foundation → prompt/context cache → native end-to-end proof → optional
Orca adapter → supervised parallelism"). Each phase already has its own
delivery report in the conversation history; this document is the
single reference for the schemas, taxonomies, contracts, and exact
commands the spec asked for at completion. Nothing here was rewritten
from scratch — every phase built on the existing repository, and every
existing capability (Codex/Claude/Qwen/Ollama/API backends, native
Ollama streaming, dynamic context sizing, project registry, persistent
architecture/milestones/backlog, memory/context intelligence/knowledge
vault, skills/MCP/plugins, capacity/cooling, git worktrees/isolation,
deterministic verification, QA/security review, dashboard, existing
tests/invariants) still works exactly as it did before.

## 1. Final architecture

```
plan.md ─▶ Architect ─▶ backlog/milestones/criteria (.agentic/project/)
                              │
             ┌────────────────▼──────────────────────────────────┐
             │  Cycle (project.py, one task):                    │
             │  restart/self-heal recovery (bootstrap_gate,      │
             │    decision_policy) → inventory refresh (stale     │
             │    only) → capacity gate → conductor → task        │
             │    contract (contract.py) → feasibility preflight  │
             │    (preflight.py, before any model call) →         │
             │    execution-engine selection (execengine.py:      │
             │    native | orca) → parallelism decision           │
             │    (parallel.py: 1 agent, or N isolated candidates  │
             │    on a real risk signal) → coder → deterministic  │
             │    gate (≤3 repairs) → QA review (≤2 rounds) →     │
             │    security review (conditional) → local commit →  │
             │    cooling (failure-class-aware, see failures.py)  │
             └────────────────┬──────────────────────────────────┘
                              ▼
             final auditor ─▶ completion notification
```

New in Phases 1–5, all additive:

| Layer | Module | Role |
|---|---|---|
| Contract | `core/contract.py` + `schemas/task-contract.schema.json` | one canonical, versioned shape every consumer (architect, conductor, worker, security policy, fscap, gate, execution engines) reads |
| Preflight | `core/preflight.py` | code-only feasibility check before any model call; 7-way result enum |
| Inventory | `core/inventory.py` | one cached, revision-fingerprinted snapshot of what the repo/machine actually has, replacing repeated re-probing |
| Failure taxonomy | `core/failures.py` | 12 stable classes, each with a documented retry/repair/fallback/cooling/blocker/escalation policy; a platform-caused failure can never masquerade as a model/provider failure |
| Filesystem capability | `core/fscap.py` | directory-aware allowed_paths/protected-path checks (`mkdir` support, case-normalised, reserved-device-name safe) |
| Cache | `core/cachestore.py` | content-addressed artifact/result cache, separate from and additive to provider-native caching |
| Execution engine | `core/execengine.py` | `native` (always available) vs `orca` (opt-in, versioned, graceful fallback) — same task contract, allowed_paths, verification, review, completion, memory, cooling, recovery either way |
| Parallelism | `core/parallel.py` | risk-based fan-out to isolated candidates with deterministic-evidence winner selection |

See `docs/adr/` for the pre-existing ADRs (Context Broker, code
intelligence, memory, knowledge, routing, skills) and
`.agentic/project/platform-upgrade-design.md` for prior phase design
notes; this document covers only what Phases 1–5 of the most recent
upgrade added.

## 2. Task-contract schema

`.agentic/schemas/task-contract.schema.json` (validated by
`core.contract.validate_contract_shape`). Produced by
`core.contract.build_task_contract(task, order, project_id, run_id)` — a
projection of the existing backlog task + work order, **not** a new
persisted file, so no migration of `backlog.yaml`/`work-order.json` is
needed. Consumed by: architect (indirectly), conductor, worker,
security policy, `core.fscap`, the deterministic gate, the QA reviewer,
the recovery engine, and both execution engines.

Key fields:

- `allowed_paths` — **the only write/security boundary**, enforced by
  `gitops.check_paths`/`safe_join`.
- `required_outputs` — the **acceptance contract**, each entry
  `{"path", "type": file|directory|glob|unknown, "required", "non_empty"}`
  — never a second write allowlist (this conflation was the root cause
  of the workspace-contract bug fixed in the sub-phase before Phase 1;
  see `core/bootstrap_gate.py`'s recovery function for the exact
  pattern it self-heals).
- `decision_classifications` — each entry classifies one embedded
  decision as `reversible_technical_choice` (auto-resolved,
  `resolution_method: autonomous_default | inferred_from_repository`)
  or `human_required` (never auto-resolved).
- `risk` — `low | medium | high`; feeds `core/parallel.py`'s fan-out
  decision.
- `completion_evidence_requirements`, `rollback_strategy` — carried
  through to the final audit and to `_revert_worktree`.

## 3. Inventory schema

`core.inventory.build_inventory(cfg, repo_root, agentic_dir, ...)` →
persisted via `core.inventory.save`/read via `core.inventory.load`,
auto-refreshed by `_run_cycle_locked` only when `is_stale()` (repository
revision changed, a manifest changed, or `INVENTORY_VERSION` bumped —
never re-scanned every cycle for no reason).

```json
{
  "inventory_version": "<int>",
  "source": "automated_scan",
  "confidence": "observed",
  "observed_at": "<iso8601>",
  "repository_revision": "<git HEAD sha or null>",
  "manifest_fingerprint": "<hash of dependency manifests>",
  "invalidation_triggers": ["repository_revision changes (new commit/checkout)",
                            "any of <manifests> changes",
                            "inventory_version bump"],
  "observed": {
    "git": {}, "languages": {}, "manifests_present": [],
    "package_managers": [], "frameworks": [], "source_directories": [],
    "tooling": {}, "docker_supabase_migrations": {},
    "cli_backend_verification": {},
    "ollama_models": {"discovered": false, "models": []},
    "mcp_servers": [], "skills": [], "writable_paths": {},
    "os_constraints": {}
  }
}
```

Every `observed.*` probe is wrapped (`_probe`) so one failing probe
(e.g. no docker installed) degrades to an empty default rather than
aborting the whole inventory build.

## 4. Preflight decision schema

`core.preflight.run_preflight(contract, task, backlog, worktree,
project_root, decisions_needed, capacity_decision, backend, inventory)`
→ `{"result": <one of 7>, "checks": [{"name","ok","detail"}, ...],
"consumes_capacity": bool, "note": ...}`. Runs entirely in code, **before
any model is invoked**.

| Result | Consumes model capacity? | Meaning |
|---|---|---|
| `feasible` | yes | proceed to the coder normally |
| `auto_repaired` | yes | a reversible gap was fixed in code before the coder ran |
| `replan_required` | no | conductor/architect must re-plan; never silently retried as-is |
| `dependency_wait` | no | a dependency/input isn't ready yet — retried next cycle, never blocked |
| `credential_required` | no | a human must supply a credential |
| `human_required` | no | a genuine business/legal decision, not a technical one |
| `platform_invalid` | no | the contract itself is structurally broken — a platform bug, blocks with a platform blocker |

`platform_invalid`/`dependency_wait`/`credential_required`/
`human_required`/`replan_required` are collectively `NO_CAPACITY_RESULTS`
— callers (`_run_cycle_locked`) short-circuit on all five without ever
calling the coder.

## 5. Failure taxonomy

`core/failures.py` — 12 classes, each with a documented policy. Four are
`PLATFORM_CLASSES` (never escalate provider circuit breakers, trust
ledger, or ordinary failure-streak cooling — routed instead to the flat
`platform_failure` cooling outcome via `cooling_outcome_for()`):

| Class | Platform class? | Retry policy | Cooling |
|---|---|---|---|
| `model_output_invalid` | no | after repair | ordinary |
| `provider_unavailable` | no | next cycle | breaker recovery estimate |
| `provider_authentication` | no | never automatic | breaker: auth required |
| `provider_capacity` | no | next cycle | provider retry-after/default |
| `deterministic_check_failed` | no | after repair | ordinary |
| `project_dependency_missing` | **yes** | next cycle | platform_failure (flat) |
| `task_contract_invalid` | **yes** | after repair (replan) | platform_failure (flat) |
| `platform_capability_missing` | **yes** | next cycle (self-heals) | platform_failure (flat) |
| `workspace_policy_denied` | **yes** | after repair (narrow allowed_paths) | platform_failure (flat) |
| `genuine_human_decision` | no | never automatic | none — paused for human |
| `execution_timeout` | no | next cycle (narrower scope) | ordinary |
| `infrastructure_failure` | no | next cycle | ordinary |

## 6. Cache architecture

`core/cachestore.py` — content-addressed store under
`.agentic/memory/cache/`, entirely separate from and additive to
provider-native caching (Anthropic `cache_control`, OpenAI automatic
prefix caching, `core.context.broker`'s `CACHE_BOUNDARY`), which
continues to work unconditionally and unchanged.

- **Artifact cache** (`ARTIFACT_CATEGORIES`): expensive-to-recompute
  *stable* content — repository inventory/map, architecture/requirements
  summaries, task dependency summaries, skill doc extracts, MCP tool
  descriptions, deterministic-check summaries, stable project context
  packages, rendered prompt-prefix text.
- **Result cache** (`RESULT_CATEGORIES`, a strict subset): only
  deterministic, code-computed facts. `FORBIDDEN_CATEGORIES`
  (`generated_code`, `review_verdict`, `qa_verdict`, `security_verdict`,
  `worker_output`, `completion_decision`) raise `CacheError`
  unconditionally — there is no override. Generated code and every
  review/approval verdict are always freshly produced and freshly
  reviewed.
- **Keys**: `compute_cache_key(**components)` hashes whatever identity
  components a caller supplies (schema_version, provider, backend_mode,
  model, role, prompt_template_version, task_contract_hash,
  stable_prefix_hash, repository_revision, relevant_file_hashes,
  inventory_version, tool_manifest_hash, skill_set_hash,
  security_policy_hash, output_schema_hash) — deterministic, same
  components → same key.
- **Invalidation**: `invalidate_dependents(dependency_name, new_value)`
  removes only entries that recorded that dependency and whose recorded
  value actually differs — never a blanket clear; `prune_expired()` for
  TTL'd entries.
- **Safety**: every write is checked against `looks_like_secret()` and a
  hint-marker denylist (credential/token/session/.env/.pem/.key/
  password/config.machine.yaml/...) before it's ever persisted.

### Cache telemetry semantics

Two honestly-separated signal families, recorded to
`.agentic/memory/cache/cache-telemetry.jsonl`:

- `application_cache_hit` / `application_cache_miss` — **this module's
  own** local cache, with an estimated `tokens_avoided` on hits. This is
  the only place "tokens saved" is ever claimed.
- `provider_cache_observation` → `{"status": "provider_cache_reported"
  | "provider_cache_unknown", "cached_tokens": <int|null>}` —
  `classify_provider_cache_report(backend_type, usage)` reports
  `provider_cache_reported` **only** when the actual response usage
  payload carried a cached-token field (never invented, never
  estimated); CLI/subscription backends are unconditionally
  `provider_cache_unknown` because nothing on that interface ever
  exposes provider-side caching to us.

`CacheStore.status()` → `{"entries", "by_category", "storage_bytes",
"application_cache_hits", "application_cache_misses", "hit_rate",
"tokens_avoided_estimated", "provider_cache_observations",
"provider_cache_reported_count", "invalidations"}`.

## 7. Orca adapter contract

`core/execengine.py` — `ExecutionEngine` interface:
`installation_detected() / detect_version() / version_supported(v) /
probe_capabilities() / smoke_test(workdir) / attach_worktree(...) /
launch_agent(SessionRequest) → SessionResult / poll_status(id) /
cancel(id) / collect_diff(id) / collect_result(id) / cleanup(id)`.

- **`NativeExecutionEngine`** — always available; a thin wrapper around
  the platform's own `_invoke_coder`, byte-identical output to
  pre-Phase-4 behaviour.
- **`OrcaExecutionEngine`** — subprocess-only (never `shell=True`),
  explicit **empty-by-default** `supported_versions` allowlist (an
  installed-but-unlisted version is never treated as compatible — "the
  latest probably works" is not a supported posture), `--version`
  detected via regex, invoked as `orca run --json --workdir <dir> --role
  <role>`, stdout parsed as `{"events": [...], "edits": [...],
  "blocked": bool, "blocker": ..., "usage": {...}}` — any output that
  doesn't parse to that exact shape becomes one opaque `raw_output`
  event, never guessed at.
- **`select_engine(cfg, caller=None, runner=None)`** — the single
  decision point. Returns native immediately unless
  `execution.preferred_engine: orca` **and** `orca.enabled: true`; even
  then, falls back to native automatically unless
  `orca.fallback_to_native: false` is set explicitly (a deliberate
  hard-fail opt-in, never the default).
- **Division of authority** (unconditional, regardless of engine):
  AgenticOS remains authoritative for task selection, the task
  contract, `allowed_paths`, model selection, capacity, verification,
  review, completion, memory, cooling, and recovery. Orca is
  responsible **only** for execution/session/worktree supervision of
  the coder role.

**Live status on this machine**: Orca is not installed (`shutil.which
("orca")` returns `None`; confirmed via `where orca` / `npm list -g`).
The "Orca present and compatible" path is verified only via a
dependency-injected test double (`tests/test_phase4_execution_engine.py`
— monkeypatched `shutil.which` + an injected `runner` callable) — never
a real binary. If a real Orca CLI becomes available, its actual
`--version` output and `--json` result schema should be checked against
this adapter's assumptions before enabling it (see §11).

## 8. Parallelism policy

`core/parallel.py`, config `parallelism:` (`max_projects_global: 4,
max_agents_global: 3, default_agents_per_task: 1` — exactly the spec's
defaults).

- **Trigger**: `decide_agent_count(task, cfg)` returns `1` (native
  single-agent, byte-identical to pre-Phase-5 behaviour) unless
  `detect_risk_signals(task)` finds a real signal, read directly off
  the task record — never inferred from free text:
  - `risk == "high"` → difficult architecture + high-risk refactoring
  - `security_relevant` → security-sensitive
  - `attempts >= 2` → repeated single-agent failure
  - UI-shaped `expected_paths` on a medium/high-risk task → competing
    UI proposals
  - high risk with a prior attempt → hard debugging
  - A trigger always asks for exactly 2 candidates, capped by
    `max_agents_global`.
- **Isolation**: each candidate gets its own git worktree/branch
  (candidate 0 reuses the worktree the platform already created for the
  real task id; candidates 1+ get `<task_id>--c<N>`), the identical
  immutable work order, an explicit backend (rotated through the
  routing chain per candidate index), and a single-shot coder → apply
  edits/scope check → deterministic gate → QA review pass — **no**
  repair loop and **no** bootstrap/structural-gate fallback per
  candidate (see §11).
- **Selection**: `select_winner` disqualifies any candidate that failed
  its deterministic gate, no matter what else it has going for it, then
  ranks survivors by `(qa_verdict==pass, acceptance_criteria_coverage,
  -lines_changed)`.
- **Integration**: `decide_integration` requires the winner to be
  unambiguous (not tied with the runner-up on every ranking signal). An
  ambiguous tie does **not** block the whole task — the tie-broken
  winner (lowest-index candidate, deterministic) still goes through
  security review and every other pipeline step normally; only the
  final `taskspace.integrate_task` merge is held for a human decision.
  Losing candidates are never merged and never deleted — preserved as
  evidence under their own branch, same retention policy as any other
  failed task worktree.

## 9. Migration plan for existing projects

**Nothing you must do.** Every new config section
(`execution.preferred_engine`/`fallback_engine`, `orca:`,
`parallelism:`) ships with defaults that reproduce exactly the
pre-Phase-1 behaviour: native engine, no Orca, no parallel fan-out for
any task without an actual risk signal. No backlog/work-order file
format changed; the task contract is a projection, not a new persisted
file; the failure taxonomy and preflight run entirely in code around
the existing cycle.

What a project picks up automatically on its next cycle, with no
action required:
- Inventory build/refresh (first cycle after upgrade builds it once;
  it's then cached and only rebuilt when the repo revision or a
  manifest changes).
- Self-healing recovery for the specific pre-fix bugs already shipped
  (`bootstrap_gate.recover_bootstrap_deadlock`,
  `recover_expected_paths_contract_bug`,
  `decision_policy.auto_resolve_reversible_decisions`) — runs at the
  top of every cycle, before any human-blocker gate.
- Tool-generated artefacts (`__pycache__`, `.pytest_cache`, etc.) are
  now filtered out of scope-violation checks and the final-audit dirty
  check everywhere that matters.

Opting in (all optional, all reversible by editing config back):
- `orca.enabled: true` + a real, tested `orca.supported_versions` entry
  — only once you actually have Orca installed and have verified its
  `--version`/`--json` output against this adapter (§11).
- `parallelism.max_agents_global` / `default_agents_per_task` — tune
  fan-out cost vs. evidence for your own risk tolerance.
- Nothing else needs opting into: caching, contract/preflight, and the
  failure taxonomy are load-bearing platform internals, on for every
  project unconditionally (same posture as the pre-existing Context
  Broker/prompt-caching machinery documented in `docs/migration.md`).

**Rolling back**: every phase is isolated to the modules listed in §1;
reverting is `git revert` on the relevant commit(s), or simply setting
`execution.preferred_engine: native` / `orca.enabled: false` /
`parallelism.default_agents_per_task: 1` / `parallelism.max_agents_global: 1`
in config without touching code at all.

## 10. Exact commands

### Windows setup (see `docs/windows-setup.md` for the full guide)

```powershell
# 1. verify the environment
py .agentic/run doctor

# 2. interactive machine configuration
py .agentic/run setup

# 3. one-command service + dashboard
agentic start
```

### Recovering `ollama-pilot`

Current live state (checked while writing this report):
`scheduler.state = "cooling"`, `failure_streak = 4`,
`cooling_reason = "failure"`, one open (non-human-only) blocker on
`t1-init-repo`: the local Ollama model itself declined to use the
`mkdir`/scope capabilities the platform already grants it, a model-
reasoning limitation, not a platform defect (the platform-side
directory-creation/`expected_paths` bug this exact task originally hit
was already fixed and is covered by
`tests/test_bootstrap_workspace_contract.py`).

```powershell
# check current state (scheduler status, blockers, progress)
py .agentic/run project show ollama-pilot

# force past the cooldown and run one cycle right now
py .agentic/run project-run --project ollama-pilot --now

# to run several cycles back-to-back instead of one:
py .agentic/run project-run --project ollama-pilot --now --max-cycles 5

# if it blocks again on the SAME reasoning-limitation pattern, either
# retry (the model sometimes succeeds on a fresh attempt) or fall back
# to a stronger backend for this project specifically, e.g. one already
# configured under providers/backends in .agentic/config.yaml:
py .agentic/run project-run --project ollama-pilot --now --primary <other-backend-id>
```

### Native acceptance tests

```powershell
# mocked-model, real-platform proof: real worktrees, real deterministic
# gate, real commits, real final audit -- everything except the model
python -m pytest tests/test_phase3_native_e2e_fixtures.py -q

# the one opt-in, fully-live fixture (real local Ollama call, no mocks
# anywhere) -- needs a real Ollama model installed and reachable
$env:AGENTIC_LIVE_SMOKE = "1"
python -m pytest tests/test_phase3_live_ollama.py -q
```

### Optional Orca smoke tests

No real Orca binary exists on this machine, so there is no live Orca
command to hand over yet -- only the always-safe test-double
verification, and the exact steps to try once a real binary exists:

```powershell
# 1. today: the dependency-injected adapter contract test (no real
#    binary needed, always runs, part of the default suite)
python -m pytest tests/test_phase4_execution_engine.py -q

# 2. once you actually have an `orca` executable on PATH: verify what
#    it reports BEFORE trusting it
orca --version
# then explicitly allowlist that exact version and enable it in
# .agentic/config.yaml:
#   orca:
#     enabled: true
#     supported_versions: ["<exact version orca --version printed>"]

# 3. confirm the platform now selects it
python -c "import sys; sys.path.insert(0, '.agentic'); from core.config import load_config; from core import execengine; cfg = load_config(); print(execengine.select_engine(cfg)[1])"

# 4. run one real cycle through it on a low-risk/disposable project and
#    diff the result against the same task run with orca.enabled: false
```

## 11. Test results

- **Focused (all Phase 1–5 test files + workspace-contract +
  repo-invariants together)**: 158 passed, 0 failed, 0 skipped.
  `python -m pytest tests/test_phase1_reliability_foundation.py
  tests/test_phase2_cache.py tests/test_phase3_native_e2e_fixtures.py
  tests/test_phase4_execution_engine.py
  tests/test_phase5_supervised_parallelism.py
  tests/test_bootstrap_workspace_contract.py tests/test_repo_invariants.py -q`
- **Full suite**: 1009 passed, 0 failed, 2 skipped (the two opt-in live
  fixtures, correctly skipped without `AGENTIC_LIVE_SMOKE=1`).
  `python -m pytest -q`
- **Security/repo invariants**: 24 passed, 0 failed.
  `python -m pytest tests/test_invariants.py tests/test_repo_invariants.py -q`

## 12. Anything not validated live (honest list)

- **Orca**: no real binary exists on this machine. The whole
  "installed and version-compatible" path is verified only via a
  dependency-injected test double. Real-Orca verification is genuinely
  outstanding.
- **Supervised parallelism**: candidates are proven isolated and
  evidence-ranked via mocked-model fixtures and this machine's real git
  worktree/branch primitives, but **not** yet exercised against a real
  local Ollama model producing genuinely different (not just
  differently-scripted) candidate outputs. `ollama-pilot`'s current
  backlog tasks are all `risk: low`-shaped scaffolding, so it has never
  actually triggered the parallel path live.
- **`ollama-pilot` full completion**: still hasn't reached
  `backlog_complete: true` end-to-end. It is self-healing correctly on
  every real attempt (two separate real platform bugs were found and
  fixed via its live runs earlier in this engagement), but the specific
  local model in use has, on its most recent live attempt, chosen a
  conservative self-block over using capabilities the platform already
  grants it — see §10 for the exact recovery/retry commands.
- **Cache hit-rate at scale**: `core/cachestore.py` is unit- and
  integration-tested (19 Phase 2 tests) but has not been observed under
  a long-running multi-cycle real project to confirm hit rates/token
  savings match the design intent — the honest telemetry exists
  (`CacheStore.status()`), it just hasn't been read back from a long
  real run yet.
- **`max_projects_global`**: defined in config with the spec's exact
  default (4) but Phase 5 didn't add new enforcement code for it —
  cross-project concurrency is already governed by the pre-existing
  multi-project fleet scheduler; whether that scheduler's existing
  limits and this new config key agree in every edge case has not been
  independently re-verified in this engagement.
