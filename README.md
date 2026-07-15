# Agentic Development OS

A provider-agnostic, trust-gated autonomous development system that lives in
`.agentic/`. It inspects the repository, picks one highest-value task,
delegates implementation to a configured **worker** model, has the result
judged by an independent **verifier** model, and gives the final vote to
**deterministic checks** — never to an AI. Autonomy is earned per skill via a
trust ledger, spending is capped by budgets, and anything sensitive waits in
a human-approval queue.

Works with OpenAI, Anthropic Claude, Qwen, OpenRouter, Ollama, any
OpenAI-compatible endpoint, or any local CLI — chosen purely by
configuration. No SDKs; the only dependency is Python 3.8+ with PyYAML.

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

## Tests

`make test` — 30+ tests, all provider calls mocked (no network, no cost),
covering provider selection/routing, env overrides, malformed output +
schema repair, refusals, timeouts, fallbacks, budgets, unknown prices,
redaction, protected paths, line limits, trust promotion/demotion, goals,
gate failures, maker/verifier disagreement, dry-run, preservation of
unrelated working-tree changes, and prompt-injection resistance at the
policy boundary.
