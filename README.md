# Agentic Development OS

A provider-agnostic, trust-gated autonomous development system that lives in
`.agentic/`. It has two modes:

1. **Full-project build** — give it a complete application plan once; it
   architects, plans milestones and a backlog, then builds the application
   across isolated ~20-minute cycles with cooling periods, capacity-aware
   scheduling, QA + conditional security review, and a final audit — then
   notifies you when the application is ready for review.
2. **Repository maintenance** — the original tick loop: find one
   highest-value task, implement, verify, queue for approval.

In both modes the final vote belongs to **deterministic checks** — never to
an AI — and no agent approves its own work.

**Backends (hybrid):**

| type | examples | cost model |
|---|---|---|
| `cli` | Codex CLI, Claude Code, Qwen CLI, future coding CLIs | your existing subscription; auth stays inside each CLI |
| `local` | Ollama | free; time/token limits still enforced |
| `api` | OpenAI, Anthropic, Qwen/OpenAI-compatible, OpenRouter | API pricing (budget-enforced) |
| `custom_command` | any local command | you decide |

No SDKs; the only dependency is Python 3.8+ with PyYAML. API keys are never
required when your selected backends are CLI or local.

## Context-efficient platform (v2)

Every model prompt is assembled by a deterministic, OS-owned **Context
Broker** — budgeted, deduplicated, provenance-tracked, with untrusted
content fenced — fed by pluggable **code intelligence** (none/native/CCE),
an OS-owned **memory** with progressive disclosure, an Obsidian-compatible
**knowledge vault**, and a pinned, reviewed **skills registry**. Roles can
route by **capability** instead of fixed provider names, reviewers stay
independent from workers, repair loops are fingerprint-bounded, API
backends use provider prompt caching where it actually exists, and the
local dashboard shows all of it. Version-1 configurations keep working
unchanged (in-memory migration).

## Multi-project operations (v2.1)

Agentic OS is installed once and manages application folders anywhere on
the machine: a central registry with explicit absolute workspaces
(`agentic project add/create/init/start…`), machine-local runtime state
under `%USERPROFILE%\.agentic-os`, per-task worktrees with file-ownership
claims and project leases, a fleet scheduler that runs up to four
projects under independent slot pools with explained waiting reasons, a
managed skills marketplace (discover → quarantine → evaluate → approve →
rollback) with a structurally restricted curator, a provider-neutral MCP
gateway (project/task-scoped, capped, audited, untrusted), project-scoped
Docker (`agentic-<id>`) and migrations-first Supabase with a production
protection ladder, accurate Claude/Qwen authentication detection, and a
one-command experience: `agentic start`. See
[projects](docs/projects.md) · [fleet](docs/fleet.md) ·
[supabase & docker](docs/supabase-docker.md) · [mcp](docs/mcp.md) ·
[authentication](docs/authentication.md) ·
[desktop wrapper plan](docs/desktop-wrapper.md) · evidence in
[docs/mp-evidence-report.md](docs/mp-evidence-report.md).

Focused documentation in `docs/`:
[architecture](docs/architecture.md) ·
[context broker](docs/context-broker.md) ·
[code intelligence](docs/code-intelligence.md) ·
[memory](docs/memory.md) ·
[knowledge vault](docs/knowledge-vault.md) ·
[skills](docs/skills.md) ·
[model routing](docs/model-routing.md) ·
[prompt caching](docs/prompt-caching.md) ·
[capacity](docs/capacity.md) ·
[dashboard](docs/dashboard.md) ·
[Windows setup](docs/windows-setup.md) ·
[troubleshooting](docs/troubleshooting.md) ·
[migration](docs/migration.md) ·
[security model](docs/security-model.md) ·
[mocked autonomous build](docs/mocked-autonomous-build.md) ·
ADRs in `docs/adr/` · evidence report in
[docs/evidence-report.md](docs/evidence-report.md).

New CLI groups (PowerShell: `py .agentic/run <cmd>`):
`context status|search|reindex|explain`,
`memory status|search|timeline|show|forget|compact|export`,
`knowledge status|rebuild|validate|open`,
`skills list|inspect|add|enable|disable|verify|remove|recommend`,
`backends discover|decisions`, `project-run --now`.

## Architecture

```
triage model          finds evidence-backed, actionable work (read-only)
      ↓
conductor model       selects ≤1 item, writes a machine-verifiable work order
      ↓
[code policy gate]    protected paths, limits, dependencies, risk → queue
      ↓
worker model          implements in an isolated git worktree
      ↓
[code path/line enforcement on every edit]
      ↓
verifier model        independent, fresh context: order + diff + check results
      ↓
deterministic gate    .agentic/scripts/verify-project — the final vote
      ↓
trust policy          watch → drafts · queue → approval · auto → autonomous
      ↓
draft | queue item | approved autonomous action (never pushed/merged)
```

Rules that hold everywhere:

- **No model approves its own work.** The verifier never sees the worker's
  conversation or self-assessment — only the work order, the diff, and the
  deterministic results.
- **The final technical verdict is deterministic.** A failed check can never
  be argued into a pass; a `verdict: uncertain` always queues for a human.
- **Policy is code.** Path rules, line limits, budgets, tool grants, and the
  contract are enforced in `.agentic/core/`, not in prompts.

Key files: `.agentic/AGENTS.md` (constitution), `.agentic/contract.md`
(MAY ACT ALONE / MUST QUEUE / MUST ALERT), `.agentic/config.yaml`
(everything configurable), `.agentic/guardrails/protected-paths.txt`.

## Installation

```sh
python -m pip install pyyaml pytest   # pytest only needed for the test suite
git init                              # if the repo is not one already
git add -A && git commit -m "init"    # worktrees need at least one commit
cp .env.example .env                  # fill in only the keys your roles use
make agent-doctor
```

No other dependencies. Windows note: `make` targets are one-liners; without
make, call `python .agentic/run <command>` directly.

## Commands

| command | what it does |
|---|---|
| `make agent-doctor` | validate deps, config, providers, repo, env vars (presence only) |
| `make agent-tick` | run one full workflow cycle |
| `make agent-dry-run` | analysis + work order, no edits |
| `make agent-queue` | show tasks awaiting human review |
| `make agent-trust` | trust tiers and statistics |
| `make agent-audit` | recent token/cost usage |
| `make agent-goals` | evaluate standing goals |
| `make test` | run the (fully mocked) test suite |

Scripts (all runnable as `python .agentic/scripts/<name>`): `invoke-model`,
`validate-response`, `verify-project`, `trust-log`, `cost-log`, `cost-check`,
`verify-goals`, `doctor`.

## Configuration

Everything lives in `.agentic/config.yaml`. Model names shipped there are
**placeholders** — assign whatever models you use; the orchestration never
depends on a specific vendor. Environment variables override any value:

```sh
AGENTIC_EXECUTION_MODE=auto            # execution.mode
AGENTIC_BUDGET_DAILY_LIMIT_USD=10      # budget.daily_limit_usd
AGENTIC_ROLE_WORKER_PROVIDER=ollama    # roles.worker.provider
AGENTIC_ROLE_WORKER_MODEL=my-model     # roles.worker.model
AGENTIC_PROVIDER_QWEN_BASE_URL=...     # providers.qwen.base_url
AGENTIC_CONFIG=/path/other-config.yaml # alternative config file
```

### Assigning models to roles

Each role (`triage`, `conductor`, `worker`, `verifier`, and their
`*_fallback` variants) names a provider, a model, sampling settings, and the
tools granted **by code** (`read_repository`, `edit_repository`,
`execute_safe_commands`). Fallbacks are role-specific (`fallback_role`) and
fire only on provider outage, rate limit, timeout, unavailable model,
context-length failure, malformed output (after one schema-repair retry), or
— only if `retry.fallback_on_refusal: true` — refusal. Auth failures never
fall back, and fallback never bypasses policy. Every switch is logged with
the original model, reason, and actual model used.

### Provider setup

- **OpenAI** — set `OPENAI_API_KEY`; provider `openai`.
- **Anthropic** — set `ANTHROPIC_API_KEY`; provider `anthropic`.
- **Qwen / any OpenAI-compatible** — set `QWEN_BASE_URL` (e.g. the DashScope
  compatible-mode URL) and `QWEN_API_KEY`; provider `qwen`. Any other
  compatible endpoint: add a provider with `type: openai_compatible` and a
  `base_url`/`base_url_env`.
- **OpenRouter** — set `OPENROUTER_API_KEY`; provider `openrouter`
  (an OpenAI-compatible endpoint at `https://openrouter.ai/api/v1`).
- **Ollama** — run Ollama locally; provider `ollama` (keyless, cost-free,
  default `http://localhost:11434/v1`).
- **Any local CLI** — `type: custom_command` with `command: [...]`; JSON in
  on stdin, JSON out on stdout (see `.agentic/providers/custom_command.py`).

**Adding another provider:** either reuse `type: openai_compatible` with a
new `base_url`, or drop a module into `.agentic/providers/` implementing
`invoke()` on `BaseProvider` and register its type in
`.agentic/providers/__init__.py`. Adapters must return the normalized
response shape and raise the typed errors in `core/errors.py`.

### Model capability differences

Adapters declare capabilities (`tool_calling`, `structured_output`,
`usage_reporting`, `refusal_reporting`, `reasoning_control`,
`context_window`). The core workflow requires **none** of them: structured
output is requested as plain JSON and validated locally against the schemas
in `.agentic/schemas/`; repository context is passed as prompt snapshots;
missing usage metadata falls back to local token estimation (marked
`token_estimate` in the ledger); timeouts are enforced by the transport; and
refusals are detected heuristically when no metadata exists.

### Example role configurations

```yaml
# 1. OpenAI-only
roles: {triage: {provider: openai, model: your-small-model},
        conductor: {provider: openai, model: your-reasoning-model},
        worker: {provider: openai, model: your-coding-model},
        verifier: {provider: openai, model: your-reasoning-model}}

# 2. Claude-only
roles: {triage: {provider: anthropic, model: your-fast-claude},
        conductor: {provider: anthropic, model: your-strong-claude},
        worker: {provider: anthropic, model: your-strong-claude},
        verifier: {provider: anthropic, model: your-fast-claude}}

# 3. Qwen-only (OpenAI-compatible endpoint via QWEN_BASE_URL)
roles: {triage: {provider: qwen, model: your-small-qwen},
        conductor: {provider: qwen, model: your-large-qwen},
        worker: {provider: qwen, model: your-coder-qwen},
        verifier: {provider: qwen, model: your-large-qwen}}

# 4. Mixed providers (independent verifier from a different vendor)
roles: {triage: {provider: openrouter, model: any-cheap-model},
        conductor: {provider: anthropic, model: your-strong-claude},
        worker: {provider: qwen, model: your-coder-model},
        verifier: {provider: openai, model: your-reasoning-model}}

# 5. Local-first with cloud fallback
roles:
  worker:
    provider: ollama
    model: your-local-coder
    fallback_role: worker_cloud
  worker_cloud: {provider: openrouter, model: any-hosted-coder}

# 6. OpenRouter-only
roles: {triage: {provider: openrouter, model: vendor/small-model},
        conductor: {provider: openrouter, model: vendor/reasoning-model},
        worker: {provider: openrouter, model: vendor/coding-model},
        verifier: {provider: openrouter, model: vendor/other-vendor-model}}
```

(Only `provider`/`model` shown; keep `temperature`, `max_output_tokens`,
`tools`, and fallbacks as in the shipped config. Model names are examples of
*shape*, not recommendations — this project makes no claims about any
model's context window, price, or capability unless you configure them.)

## Budgets

`budget:` sets a daily USD limit, per-run USD limit, and per-run token caps.
Limits are checked before a run, before every invocation (including
fallbacks), and after every invocation; hitting one stops the run safely and
alerts at `warning_percentage`. Costs use real provider usage metadata when
reported. **Prices are not hard-coded** — fill the `pricing:` table (USD per
1M tokens) yourself; unknown prices follow `unknown_price_policy`:
`block` (default) refuses the call, `warn` proceeds recording cost 0 with a
warning, `allow` proceeds silently. Ollama and other `cost_free: true`
providers always cost $0. Audit with `make agent-audit`; ledger:
`.agentic/memory/usage.tsv`.

## Trust tiers

Per **skill** (stable kebab-case name), never per model — switching
providers keeps history. Ledger: `.agentic/memory/trust.tsv`.

- `watch` — <10 runs or <90% pass rate → produces drafts only
- `queue` — intermediate → verified work waits for approval
- `auto` — ≥20 runs and ≥95% → autonomous **only** when
  `execution.mode: auto`, and even then never pushes/merges/deploys

Every attempt records pass/fail; a deterministic-gate failure is a failure;
two consecutive failures force `watch`; demotion is automatic and alerts.
Sensitive skills are capped at `queue` unless listed in
`trust.sensitive_auto_allowed`. Optional per-model stats:
`trust.track_by_model` → `memory/trust-by-model.tsv`.

## Contract rules

See `.agentic/contract.md`. Highlights: dependency changes, auth, payments,
migrations, deploy config, secrets, public APIs, and anything matching
`guardrails/protected-paths.txt` (extensible via
`contract.extra_protected_paths`) always queue. Alerts (budget, secrets,
repeated failures, goal violations, malformed providers, protected-path
attempts, demotions) go to `memory/STATE.md`, `memory/decisions.jsonl`, and
stderr.

## Standing goals

`.agentic/goals/*.yaml`, each with a **deterministic predicate** (a command;
exit 0 = satisfied). Evaluated every tick and by `make agent-goals` /
`verify-goals` (nonzero exit on violation), with a per-goal timeout; results
land in `memory/goal-ledger.tsv`. Retiring a goal requires a human editing
`status: retired`.

## Manual execution, dry runs, recovery

- `make agent-dry-run` — full analysis and a work order, zero edits.
- `make agent-tick` — one cycle. Drafts live on `agentic/<run-id>` branches
  with worktrees preserved under `.agentic/worktrees/` when review is needed.
- Approve a queued item by merging its branch yourself, then delete the queue
  JSON; reject by deleting the queue file and
  `git worktree remove .agentic/worktrees/<run-id> && git branch -D agentic/<run-id>`.
- If the repo starts with failing checks, the first tick records a baseline
  (`memory/baseline.json`); the gate then blocks *new* regressions without
  claiming the repo is healthy. Delete the file to re-baseline.
- Diagnostics always start with `make agent-doctor`; run artefacts and check
  logs are under `.agentic/runs/<run-id>/`.

## Scheduling

Nothing is scheduled automatically. Copy-paste examples for **cron**,
**GitHub Actions**, and a **systemd timer** are in
`.agentic/examples/schedule/`. All scheduled runs pass through the same
contract, budgets (`cost-check` gate), trust rules, and deterministic checks
as manual runs.

## Optional features (off by default)

`optional_features:` flags in config are extension points: quorum triage
(N triage calls, intersect findings), metric ratchets (standing goals over
numeric thresholds), breaker/builder sparring, weekly failure analysis
(`prompts/compost.md` is ready — schedule
`invoke-model --role conductor --prompt-file .agentic/prompts/compost.md`),
notifications, and GitHub issue/PR integration (requires granting `gh` write
commands to the safe-command allowlist — a contract change a human must
make). Enable by flipping the flag and wiring the corresponding script into
your scheduler; none of them weaken the contract, budget, or gate.

## Security model and limitations

- Repository content, issues, commit messages, test output, and diffs are
  **untrusted input**. Prompts instruct models to ignore embedded
  instructions, but the real boundary is code: tool grants, path rules,
  line limits, budgets, and the queue cannot be altered by any model output.
  A prompt-injected work order asking for `.env` or `**` access is queued,
  never executed (see `tests/test_policy_gate.py`).
- Secrets exist only as environment variables; logs and prompts pass through
  a redactor; the doctor reports presence, never values. `.env` is
  git-ignored.
- Worker commands run only if they appear verbatim in
  `execution.safe_commands` (empty by default).
- Residual risks you accept when enabling `mode: auto`: models can still
  write flawed-but-passing code inside allowed paths; deterministic checks
  are only as good as your test suite; redaction is pattern-based and cannot
  catch every secret shape; `verify-project` executes repository-defined
  test commands, which is code execution by definition — run in a container
  or CI when the repository content is not fully trusted.

## Full-project build mode

```sh
make agent-setup                  # detect CLIs/Ollama/APIs, choose routing,
                                  # write .agentic/config.machine.yaml
make project-start PLAN=plan.md   # architect: plan -> milestones + backlog
make project-run                  # run the next eligible cycle
make project-status               # scheduler + progress + blockers
make project-pause / project-resume
make project-review               # run the final audit now
make agent-capacity               # capacity ledger + next-cycle estimate
make agent-backends               # circuit-breaker states
```

(Direct equivalents: `python .agentic/run setup|project-start|project-run|…`.)

**How a cycle works:** the capacity manager decides start/reroute/wait →
conductor turns one backlog task into a bounded work order → coder implements
it in the persistent `agentic/project` worktree (CLI coders edit files
directly under a `workspace-write` sandbox; API/local coders return
structured edits applied by the OS) → deterministic checks run (a repo with
**zero** checks blocks, always) → up to 3 repair attempts (with a structured
handoff to the next backend if the current one runs out mid-task) → QA
reviewer judges the diff → security reviewer runs only when the change
touches security-relevant territory → the cycle commits, project state
updates, and cooling starts (30 min after success or ordinary failure;
dynamic after rate/usage limits, clamped to 5–360 min).

**Resume after anything:** all state (backlog, scheduler, breakers, capacity
ledger) is persisted; after a process/computer restart, exhausted CLI quota,
or an interrupted cycle, `project-run`/`project-resume` continues exactly
where it stopped and never regenerates completed work. Long waits are never a
blocking sleep — the next eligible time is persisted; re-invoke manually or
via a timer you install yourself (see `.agentic/examples/schedule/`).

**Interaction modes** (`interaction.mode`): `cycle_review` notifies after
every cycle; `milestone_review` notifies per milestone; `completion_only`
(default) notifies only for: project complete, a genuine human-only blocker,
all backends unavailable beyond the window, or a security-critical decision.

**Completion** is evidence, not an empty backlog: the final audit requires
milestones done, no open blockers, mandatory checks green, no uncommitted
changes, no committed secrets, `.env.example` where needed, and an
independent final review — results in `.agentic/project/final-audit.yaml`.
The finished application sits on the `agentic/project` branch for **you** to
review and merge; the OS never pushes, merges, or deploys.

## CLI subscription backends

Authentication always stays inside each official CLI — the OS never reads,
copies, prints, or commits cached tokens; health checks use the CLI's own
status commands (e.g. `codex login status`).

- **Codex:** install and `codex login`, then run setup. Invocations are
  `codex exec --ephemeral --json --sandbox <mode> --ask-for-approval never
  --cd <workspace>`; `workspace-write` only for the coder role, `read-only`
  for everyone else; `--dangerously-bypass-approvals-and-sandbox` is on a
  hard forbidden list.
- **Claude Code / Qwen CLI:** version-detected, template-configured adapters
  (`backends.claude` / `backends.qwen` in config) — exact flags live in
  configuration, are validated at setup, and require a passing
  non-interactive smoke test before autonomous use. Interactive TUIs are
  never automated by keystroke injection.
- **Ollama:** detected via `ollama list`; pick an installed model during
  setup. Free, but time/token limits still apply.

## Capacity, cooling, circuit breakers

Subscription CLIs rarely expose exact quotas, so every capacity figure is
labelled `reported`, `estimated`, or `unknown` — **estimates are local
approximations and are never presented as provider-reported quota.** Before
each cycle the manager estimates conductor+coder+QA+security+repair-reserve
tokens (× `capacity.safety_multiplier`, default 1.35) against history and
your self-imposed `limits:` (null = not configured; provider limits are never
invented), then decides start / reroute / wait / human-required. Rate-limit
and usage-limit events open per-backend circuit breakers with explicit
retry-after/reset parsing when available, or growing historical recovery
estimates when not; re-enabling requires the wait plus a health check.
Fallback routing is ordered and logged, and never fires for auth failures or
refusals. One-run override: `project-run --primary codex --fallback claude
--fallback ollama`.

## Permissions and security boundaries

Autonomous means non-interactive inside a pre-authorised boundary: read/edit
repository files, worktrees and local branches/commits, allowlisted commands,
tests/lint/build, state updates, fallback switching, pause/resume. Never:
push, merge, deploy, message anyone, touch DNS/cloud, read credentials,
access unrelated directories, or destructive external actions. Commands are
argv arrays — `shell=True` exists only in the execution-policy module for
explicitly `shell_required: true` admin-authored config entries; commands
from model output must match the allowlist verbatim and never get a shell.

## Troubleshooting / disabling automation

- `make agent-doctor` — full readiness report (backends, auth status without
  credential content, routing, breakers, scheduler, project, capacity
  confidence). Warnings are never presented as readiness.
- Stuck cycle: locks staler than 2h self-break; `make project-status` shows
  the cooling reason and next run time.
- Backend stuck "unavailable": inspect `make agent-backends`; delete
  `.agentic/memory/backends.json` to reset breakers.
- **Disable automation:** `make project-pause` stops cycles; remove any
  timer/cron entry you installed; nothing runs unless something invokes
  `project-run`. Scheduling is never activated automatically.
- Clean distribution: `python .agentic/run package` builds a zip that
  excludes runs, worktrees, machine config, memory ledgers, and caches.

## Control Centre dashboard

A local web dashboard — the **Agentic OS Control Centre** — provides a
control and observation layer over the same orchestration engine. It never
reimplements orchestration logic; every number it shows comes from the
Python modules and the YAML/JSON/TSV state they maintain.

```powershell
py .agentic/run ui              # start on http://127.0.0.1:8765
py .agentic/run ui --port 9000  # different port
py .agentic/run ui --no-open    # don't open the browser
```

The command starts a loopback-only FastAPI service, serves the built
frontend, prints the URL, optionally opens your browser, and shuts down
cleanly on Ctrl+C. If the preferred port is busy it scans upward safely.

**Pages:** Overview (live orchestration rail: Architect → Conductor → Coder
→ Gate → QA → Security → Commit), Projects (create/preview/start plans,
architecture, backlog, acceptance criteria, decisions, final audit), Build
Control (start/pause/resume/audit with disabled-state explanations),
Agents (roles, permissions, routing editor), Backends (detection, auth
status as reported by each CLI, breaker states, confirmed smoke tests),
Capacity (reported vs estimated vs unknown — estimates are never presented
as provider quotas), Verification (deterministic checks, baseline vs
regression, QA/security verdicts, full logs), Activity (filterable audit
trail), Settings (validated machine-local configuration).

A command palette (`Ctrl+K`) exposes navigation and safe project actions;
invalid actions are disabled with the reason shown.

### Building the frontend

The dashboard frontend is a Vite + React + TypeScript app in `ui/`
(requires Node.js 20+):

```powershell
cd ui
npm install
npm run build     # writes ui/dist, served by `py .agentic/run ui`
```

Development mode (hot reload, API proxied to the Python service):

```powershell
py .agentic/run ui --dev --no-open   # terminal 1: API on 8765
cd ui; npm run dev                   # terminal 2: UI on 127.0.0.1:5173
```

Frontend checks: `npm run test`, `npm run typecheck`, `npm run lint`,
`npm run build` (all inside `ui/`).

### Dashboard security boundaries

- Binds to `127.0.0.1` only; non-loopback hosts are refused outright
  because no remote authentication layer exists (by design).
- Every request must carry a loopback `Host`; state-changing browser
  requests must carry a loopback `Origin` (CSRF defence).
- No arbitrary command endpoint and no arbitrary filesystem endpoint:
  plan paths must resolve inside the repository root; log access is
  confined to `.agentic/runs/` with strict name validation.
- All content that could contain model/CLI output is redacted before it
  reaches the browser; credential files and environment values are never
  read or returned; `auth unknown` is never treated as authenticated.
- The dashboard has **no push, merge or deploy capability** — reviewing
  and merging `agentic/project` stays a human act in your terminal.
- Smoke tests and breaker resets require explicit confirmation (smoke
  tests consume real subscription allowance or API cost).
- Every state-changing dashboard action is appended to the audit trail
  (`decisions.jsonl`, `source: dashboard`).
- Settings persist only to `.agentic/config.machine.yaml` (git-ignored);
  credential-shaped keys and values are rejected server-side.

### Dashboard configuration

Set in Settings (validated) or `.agentic/config.machine.yaml`:

```yaml
ui:
  port: 8765          # default dashboard port
  open_browser: true  # open browser on start
  theme: dark         # dark | light
  reduced_motion: system   # system | true | false
```

**Windows notes:** run with `py .agentic/run ui` from PowerShell; paths
with spaces (incl. OneDrive folders) are supported; no Make or Bash is
required. Stop with a single Ctrl+C.

**Troubleshooting:** if the page shows "built frontend not found", build
`ui/` as above (the API still runs without it). If the live badge shows
"connection lost", the Python process stopped — restart it; the browser
reconnects and replays missed events. To disable the dashboard entirely,
simply never run `run ui` — nothing starts it automatically. To clear only
dashboard cache safely, delete `.agentic/memory/ui-operations.json` (the
list of recent dashboard-initiated operations) — project state, capacity
ledgers and the audit trail are untouched.

The design system behind the interface is documented in
`design-system/MASTER.md` (tokens, typography, status semantics, motion
and accessibility rules) with page-specific notes in
`design-system/pages/`. The dashboard requires `fastapi` and `uvicorn`
(`pip install fastapi uvicorn`).

## Tests

`make agent-test` — 103 tests, all CLI processes and API transports mocked
(no network, no subscription quota, no cost). Live smoke tests are opt-in and
clearly labelled: `AGENTIC_LIVE_SMOKE=1 pytest tests/test_live_smoke.py`.

Coverage spans the original maintenance mode (providers, budgets, trust,
goals, gates, injection resistance) plus configuration layering, the setup
wizard, CLI adapters (Codex/Claude/Qwen/Ollama) with credential-isolation
guards, fallback routing, circuit breakers, retry-after parsing, capacity
estimation and start decisions, cooldowns, scheduler persistence, overlap
locks, restart/resume, dependency ordering, conditional security review,
repair limits, structured handoff, zero-check blocking, budget-exception
containment, shell-execution policy, secret scanning, no-push/merge/deploy
guards, notification policy, the final audit, and a mocked end-to-end
complete-project build.
