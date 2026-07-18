# Evidence Report — Multi-Project Operational Upgrade

Date: 2026-07-18 · Baseline `5fa4262` (280 passed / 1 skipped) → 13 local
commits (`253be09`…), one per phase, never pushed. Final suite:
**416 passed, 1 skipped (opt-in live smoke), 0 failed** (136 new tests
across 13 new test modules); frontend vitest **28 passed** (4 new
component tests), frontend build clean; doctor OK with truthful
per-backend auth/smoke/readiness. No test contacts a provider, database,
docker daemon, or network.

| # | Definition of done | Implementation | Tests |
|---|---|---|---|
| 1 | Manage external project folders | `core/registry.py`, `core/projectops.py`, `project` CLI, `bin/agentic.*` | `test_registry.py` (22) |
| 2 | Explicit absolute workspaces | canonical path resolution + ProjectPaths (`project._paths` runtime overlay) | `test_registry.py::test_project_cfg_overlay…`, `test_state_boundaries.py` |
| 3 | Platform repo not implicitly the application | platform-repo guard + `adopt-legacy` opt-in | `test_registry.py::test_platform_repository_refused…` |
| 4 | Four projects registered and scheduled | `core/fleet.py` | `test_fleet.py` (14), `test_multiproject_e2e.py` |
| 5 | Concurrency limits enforced | SlotManager pools (model/backend/docker/local/test) | `test_fleet.py::test_model_slots…`, `::test_per_backend_slots` |
| 6 | Projects isolated | per-project runtime dirs, memory namespaces, indexes, logs | `test_registry.py::test_two_projects…`, E2E §20 |
| 7 | Worktrees + leases prevent conflicts | `core/taskspace.py` (claims, exclusive classes, leases) | `test_taskspace.py` (12) |
| 8 | Skill discovery without installation | `SkillMarket.discover` (metadata only) | `test_skillmarket.py::test_discover_stores_metadata_only` |
| 9 | Pinned/scanned/evaluated/approved skills | quarantine + checksum + injection scan + offline eval + explicit approve | `test_skillmarket.py` (14) |
| 10 | Updates require review | `check-updates`/`compare`/approve; never silent; rollback preserved | `::test_update_flow_never_automatic`, `::test_rollback…` |
| 11 | MCP project- and task-scoped | `core/mcp.py` scoping + task_config narrowing | `test_mcp.py` (13) |
| 12 | Reproducible local Supabase migrations | migrations-first rule + reset/seed/types workflow | `test_docker_supabase.py::test_local_workflow_success`, `::test_migration_first_rule` |
| 13 | Production database protected | environment policy ladder; resets denied; approval gates; dry-run evidence | `::test_production_mutation_and_reset_denied`, `::test_remote_dry_run…` |
| 14 | Docker project-scoped | `agentic-<id>` compose names + op allowlist + screens + build lock | `::test_compose_project_name_isolation`, `::test_unsafe_operations_rejected` |
| 15 | Claude auth via supported status command | `core/authx.py::claude_auth_detail` | `test_authx.py` (7 claude cases incl. conflict/expired/old CLI) |
| 16 | Qwen CLI vs Ollama Qwen separated | honest `unverified` CLI + `local_ok` Ollama + routing exclusion until smoke-verified | `test_authx.py::test_unverified_qwen_cli_excluded…`, E2E §16–17 |
| 17 | Dashboard explains paths/branches/states/waits | `ui/portfolio.py` + Portfolio/MCP pages | `test_ui_portfolio.py` (10), `portfolio.test.tsx` (4) |
| 18 | `agentic start` usable localhost experience | `core/service.py` + start/stop/restart/status/logs/dashboard | `test_service.py` (11) |
| 19 | Loopback-only by default | serve.py binding + Host/Origin middleware (pre-existing, re-verified on every new mutation) | `test_ui_portfolio.py::test_portfolio_mutations_loopback_guarded`, `test_ui_boundary.py` |
| 20 | Desktop-wrapper-ready | readiness/version/shutdown endpoints + `docs/desktop-wrapper.md` (Tauri 2 recommended) | `test_ui_boundary.py` (3) |
| 21 | Existing tests pass | no orchestration rewrite; two invariants deliberately widened (local integration merge confined to taskspace; loopback health probe in service.py) with tests documenting why | full suite green |
| 22 | New tests without provider quota | all runners/sessions/transports injected | 112 new tests, 0 live |
| 23 | Package clean | runtime home lives OUTSIDE the repo; package exclusions unchanged | `test_state_boundaries.py`, `test_invariants.py::test_package_excludes…` |
| 24 | No push/deploy/production mutation | invariants + E2E §21 (`supabase db push` is the dry-run-gated migration apply, documented carve-out) | `test_invariants.py`, `test_scheduler_project.py` §47 |
| 25 | This report | — | you are reading it |

## Honest limitations

- `agentic start` spawns the dashboard service; fleet cycles still run
  via `fleet tick` (manually or a scheduled task) — a resident scheduler
  loop is future work.
- The fleet models the docker-build pool at the slot level; adapters
  additionally hold the machine-wide build lock, but the planner does not
  yet predict which task will need a build.
- MCP OAuth flows are guided, not embedded; SSE servers are invoked via
  their HTTP POST endpoint only.
- Skill/marketplace sources are local mirrors by design; the OS performs
  no network fetches.
- Claude's `auth status` output format is parsed defensively (JSON,
  text, exit code) but future CLI changes may require a parser update —
  failures degrade to `probe_failed` with guidance, never a false
  "authenticated".
- `project start` runs the architect synchronously in the CLI; the
  dashboard triggers init (model-free) but delegates architecting to the
  CLI/fleet path.
