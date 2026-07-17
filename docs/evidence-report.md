# Final Evidence Report — Platform Upgrade

Date: 2026-07-18 · Baseline: `140ecc8` → phases `4381ad9…` (one local
commit per phase, never pushed).

Test evidence: full suite `py -m pytest -q` — **280 passed, 1 skipped
(opt-in live smoke), 0 failed** (281 collected; 142 at baseline);
frontend `npx vitest run` — **24 passed**;
`py .agentic/run doctor` — OK (warnings only for unset optional API keys /
placeholder tick-mode models). No test contacts a real provider.

| # | Definition-of-done requirement | Implementation | Tests |
|---|---|---|---|
| 1 | Existing behaviour compatible / documented migration | `core/migrate.py`, defaulting accessors in every subsystem; `docs/migration.md` | all pre-existing suites still pass unchanged (142 baseline tests) |
| 2 | Ordinary tests run without provider usage | mocked Transport/Runner/Caller/ScriptedAdapter fixtures | `tests/conftest.py`; only `test_live_smoke.py` (skipped) is live |
| 3 | Full test suite passes | — | 231 passed / 1 skipped, exit 0 |
| 4 | Doctor reports truthful readiness | `core/doctor.py` (version, code-intel state, backend auth, fatal-vs-warning levels) | `tests/test_config_setup.py`, manual doctor run |
| 5 | Plan drives a mocked build to completion | `core/project.py` | `tests/test_e2e_autonomy.py::test_full_autonomous_build` |
| 6 | Every model call uses the Context Broker | `core/context/`, `make_caller`, `run_tick.call` | `test_context_broker.py::test_run_tick_prompts_built_by_broker`, `::test_project_caller_goes_through_broker`, E2E ledger-count assert |
| 7 | Packages stay within budget | `broker.build` + `_shrink_to_budget` | `test_context_broker.py::test_budget_never_exceeded`, `::test_output_reserve_protected…`, `::test_mandatory_overflow_fails_loudly` |
| 8 | Code retrieval with fallback | `core/codeintel/` (none/native/cce) | `test_codeintel.py` (15 tests incl. fallback, staleness, exclusions, Windows paths, benchmark) |
| 9 | Memory progressive disclosure | `core/memsvc.py` | `test_memory.py` (14 tests: layers, isolation, redaction, supersession, expiry, recovery) |
| 10 | Obsidian-compatible knowledge files | `core/knowledge.py` | `test_knowledge.py` (12 tests: frontmatter, stability, user sections, conflicts, links, `.obsidian`) |
| 11 | Skills pinned, reviewed, on-demand | `core/skillreg.py` + `.agentic/skills/builtin/` | `test_skills.py` (12 tests: pinning, checksums, review gates, progressive loading) |
| 12 | Capability + availability routing | `core/routing.py`, `backends.routing_chain` | `test_routing.py` (13 tests) |
| 13 | Worker/reviewer different providers | `reviewer_different_from_worker` policy | `test_routing.py::test_reviewer_independence` (+unsatisfiable case) |
| 14 | Repair loops bounded | det-attempts + review rounds + fingerprints in `project.py` | `test_council.py` (8 tests), `test_scheduler_project.py` |
| 15 | Capacity reserves room for review | `capacity.estimate_cycle_tokens` review reserve | `test_capacity_scheduling.py::test_review_reserve…`, `::test_insufficient_reported…` |
| 16 | Caching only where supported | `providers/anthropic.py` (cache_control), `openai.py` (prefix only), CLIs strip marker | `test_caching.py` (12 tests) |
| 17 | CLI caching/capacity honestly labelled | `cli_base.compose_prompt`, capacity confidence labels | `test_caching.py::test_cli_prompt_stripped…`, `test_routing.py::test_discovery_reports_honestly` |
| 18 | Dashboard reflects persisted state | `.agentic/ui/intel.py` + endpoints + 5 pages | `test_ui_intel.py` (9), `ui/src/test/intel.test.tsx` (7) |
| 19 | No credentials/runtime data in packages | `run package` exclusions, redaction | `test_invariants.py::test_package_excludes…`, redaction tests |
| 20 | No push/merge/deploy | absent from all autonomous code | `test_invariants.py::test_no_push_merge_deploy…` |
| 21 | This report | — | you are reading it |

## Security requirements → tests

Shell prohibition (`test_invariants.py::test_model_commands_never_shell`,
`::test_shell_true_only_inside_execpolicy`); allowlist immutability
(`::test_allowlist_is_config_only`); path confinement
(`::test_workspace_confinement_everywhere`, CCE/vault/skill suites);
credential hygiene (memory/vault redaction tests, UI no-credential test,
index exclusions); untrusted-content supremacy of policy
(`test_context_broker.py::test_untrusted_content_cannot_enter_policy…`,
`test_skills.py::test_skill_items_untrusted_and_policy_safe`); auth
no-fallback (`test_routing.py::test_auth_failure_excluded…`,
`test_e2e_autonomy.py::test_auth_failure_stops_without_fallback`);
localhost dashboard (`test_ui_api.py`, `test_ui_intel.py::test_new_
mutations_are_loopback_guarded`); auditability
(`test_invariants.py::test_cycle_actions_logged`).

## Remaining limitations (honest)

- The CCE adapter is written against a **mocked contract**; the real
  engine's CLI surface/licence were not verifiable from here. Live
  integration is detection-gated and falls back to `native`.
- `skills add` installs from local pinned checkouts only; fetching remote
  archives is deliberately out of scope (network stays outside the OS).
- Subscription CLI capacity/caching remain estimates/unknown by design;
  parsing of rate-limit hints is best-effort.
- The native retriever is lexical, not semantic; the benchmark is a local
  synthetic measurement.
- Capability routing scores capabilities from configuration + observed
  history; it cannot measure a model's true quality.
- `ui_designer` shares the coder toolchain; it differs by routing, skills
  and role identity, not by a separate execution path.
- Tick-mode (`run tick`) uses the broker but not code-intelligence
  retrieval (it keeps its bounded snapshot); project mode has full
  retrieval.
