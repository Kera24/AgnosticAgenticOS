# Multi-Project Operational Upgrade — Gap Analysis (Phase 0)

Date: 2026-07-18 · Baseline commit: `5fa4262` · Baseline tests: 280
passed, 1 skipped (opt-in live smoke), 0 failed · doctor: OK · dashboard
API + frontend suites green (`test_ui_api.py` 39, `test_ui_intel.py` 9,
vitest 24).

## 1. What already exists (verified, reused as-is)

| Area | Where | State |
|---|---|---|
| CLI | `.agentic/run` (argparse; project-*/context/memory/knowledge/skills/backends/ui/…) | works, single project |
| Orchestration engine | `core/project.py` (cycle loop), `core/orchestrator.py` (tick) | works — will NOT be rewritten |
| Context Broker | `core/context/` | complete; only prompt assembler |
| Code intelligence | `core/codeintel/` (none/native/cce) | complete |
| Memory | `core/memsvc.py` — `project_id` column already namespaces records | complete; namespace = `cfg.project.name` |
| Knowledge vault | `core/knowledge.py` (root = `<agentic_dir>/knowledge`) | complete; dir injectable |
| Skills registry | `core/skillreg.py` — pinned/checksummed/reviewed/enable/disable/verify/remove | solid base; lacks discovery/quarantine/eval/curator/updates/rollback/projections |
| Routing | `core/routing.py` (simple/per_agent/capability + decisions) | complete |
| Capacity/breakers/scheduler | `core/capacity.py`, `breaker.py`, `scheduler.py` | complete for ONE project |
| Dashboard | `.agentic/ui/` FastAPI + `ui/` React; loopback/Origin guards | complete, single project |
| Exec policy | `core/execpolicy.py` (argv-only for models, allowlist) | complete; Docker/Supabase adapters will build on it |
| Config layering + v2 migration | `core/config.py`, `core/migrate.py` | complete |
| Auth probes | `providers/cli_base.py::auth_status` via `auth_probe_args` | codex+ollama OK; claude has no probe configured → "unknown"; qwen probes stale |

## 2. Single-project assumption (confirmed)

- `config.repo_root(cfg)` defaults to `AGENTIC_DIR/..` — the platform
  repository IS the application.
- All state hangs off `config_mod.AGENTIC_DIR`: `project/` (plan,
  backlog, progress…), `memory/` (scheduler.json, capacity TSVs,
  memory.db, context-ledger, code-index, backends.json), `worktrees/
  project`, `runs/`, `knowledge/`, `skills/registry.yaml`.
- `project.py::_paths(cfg)` is the single choke point that builds these
  paths — the extension seam for Phase 2.
- Memory records are already project-scoped by name; worktree branch
  `agentic/project` is fixed per repo. One `ProjectLock` prevents
  concurrent cycles for the one project.

## 3. Missing entirely

- Central project registry, ProjectRecord schema, `project add/create/
  relink/archive/…` commands, `agentic` executable wrapper.
- ProjectPaths service / runtime-root separation (machine-local home).
- Per-task worktrees, file-ownership records, overlap detection, leases.
- Multi-project fleet scheduler, slot pools, waiting reasons.
- Skill lifecycle states (discovered/quarantined/…), curator, update
  compare/rollback, provider projections, registry sources.
- MCP: nothing exists (no gateway, no records, no transports).
- Docker adapter, Supabase adapter, environment policy.
- Accurate Claude (`claude auth status`) and honest Qwen detection;
  credential-conflict reporting; autonomous-readiness gating.
- `agentic start/stop/restart/status/logs`, single-instance lock,
  detached service, pid/log files.
- Desktop-wrapper boundary doc; local shutdown endpoint.
- Four-project mocked E2E.

## 4. Extension plan (no competing systems)

1. **Registry** (`core/registry.py`): JSON at the machine-local runtime
   home (`%USERPROFILE%\.agentic-os\`, override `AGENTIC_HOME`), atomic
   writes, schema v1, authorised-roots policy. `project_cfg(base, rec)`
   overlays: `project.name=<id>`, `project.repository_root=<abs root>`,
   `runtime.project_dir=<home>/projects/<id>` — then the EXISTING engine
   runs unchanged against redirected paths.
2. **ProjectPaths** = extend `project._paths(cfg)` to honour
   `cfg["runtime"]`; every state dir (state/memory/runs/worktrees/
   knowledge/skills-selection) resolves through it. No CWD reliance in
   core logic (already true; `run` resolves the registered project).
3. **Worktrees/leases** (`core/leases.py` + extension of
   `ensure_project_worktree`): per-task worktrees under
   `<home>/projects/<id>/worktrees/<task-id>`, ownership manifest,
   overlap check, lease files with expiry.
4. **Fleet scheduler** (`core/fleet.py`): pure `plan()` (slot pools,
   fairness, priorities, waiting reasons, persisted decisions) +
   `run()` executor with injectable per-project runner (tests never run
   real cycles).
5. **Skills marketplace**: extend `core/skillreg.py` with states,
   quarantine storage, offline registry sources (local dir/git checkout/
   internal index; skills.sh + Anthropic marketplace are configured
   registry entries whose fetch stays outside the OS — discovery reads
   local mirrors/fixtures), deterministic curator (code, not an LLM),
   update compare + rollback, projections.
6. **MCP gateway** (`core/mcp.py`): stdio JSON-RPC via execpolicy,
   HTTP(S) via the existing `providers.base.default_transport` (keeps
   the no-new-network invariant), machine-local records, tool policy,
   task-scoped config generation, pass-through writers.
7. **Docker/Supabase** (`core/dockerx.py`, `core/supabasex.py`):
   allowlisted argv operations, compose project `agentic-<id>`,
   environment policy table, migration-first rule, all runner-mocked.
8. **Auth** (`providers/cli_base.py` + `cli_configured.py` +
   backends config): `auth_detail()` with the spec's states, claude
   `auth status` JSON+text parsing, qwen honest "unverified" +
   guidance, env-conflict detection, readiness gating in doctor/
   routing.
9. **Dashboard**: new endpoints in `.agentic/ui/` (portfolio, fleet,
   mcp, auth detail) + frontend Portfolio/MCP additions reusing the
   existing design system.
10. **Service lifecycle**: `run start/stop/restart/status/logs` +
    `bin/agentic.cmd`/`agentic.ps1` wrappers, pid+lock+log files in the
    runtime home, detached spawn, health wait, port handling.
11. **Desktop boundary**: `docs/desktop-wrapper.md` (Tauri vs Electron),
    protected `/api/v1/shutdown`.
12. **E2E**: `tests/test_multiproject_e2e.py` four-project scenario.

## 5. Migrations & backward compatibility

- No registry file ⇒ behaviour identical to today (platform repo as the
  implicit project); `_paths` falls back to `AGENTIC_DIR`.
- `project migrate-legacy` (part of `project add --adopt-legacy`) can
  register the existing in-repo project and leave its state in place.
- Registry schema carries `schema_version: 1` with a migration hook.
- Skill registry gains a `state` field defaulted from `enabled/reviewed`
  (enabled+reviewed → `enabled`; else `approved`/`discovered`).
- No existing file formats change; new state lives in new files.

## 6. Risks / decisions taken

- Fleet concurrency uses threads over per-project state dirs; shared
  machine files (registry, slots, leases) get lock-file guarded atomic
  writes. Breaker boards stay per-project (duplicated cooling info is
  acceptable; cross-project backend slots come from the fleet pools).
- Marketplace network fetches remain OUTSIDE the OS (admin clones /
  local mirrors), consistent with the existing skills ADR 0006.
- The Qwen CLI is reported honestly as unverified-until-smoke-tested and
  excluded from autonomous routing until then; Ollama Qwen models are a
  separate backend and unaffected.
