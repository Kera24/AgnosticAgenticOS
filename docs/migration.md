# Migration from the Previous Version

Nothing you must do. Version-1 configurations keep working: the loader
(`core/migrate.py`) normalises them to version 2 **in memory**, filling
defaults for the new sections (`context`, `memory`, `knowledge`,
`skills`, `caching`, extended `routing`). Your file is never rewritten;
doctor reports the effective version and which defaults were applied.

## What changed underneath

- Every model prompt now goes through the Context Broker. Existing
  prompts/roles/schemas keep their meaning; shared policy files are
  injected by the broker instead of `load_prompt`.
- The repair loop split into deterministic attempts (3) and model-review
  rounds (`cycle.maximum_review_rounds`, default 2), with identical-
  failure short-circuiting. A task that previously burned all attempts on
  the same diff now blocks earlier with a clearer reason.
- New machine-local runtime stores (all git-ignored, safe to delete):
  `memory/memory.db`, `memory/context-ledger.jsonl`,
  `memory/routing-decisions.jsonl`, `memory/code-index/`,
  `.agentic/knowledge/` (regenerable via `knowledge rebuild`),
  `.agentic/skills/registry.yaml`.
- Capacity TSV ledgers are unchanged in format; older rows keep working.

## Opting into the new capabilities

- `routing.mode: capability` + `routing.agents` (see
  `.agentic/examples/config.example.yaml`).
- `context.code_intelligence.provider: cce` if you install CCE.
- `providers.<name>.cache_ttl: "1h"` for Anthropic API long-TTL caching.

## Rolling back

Each phase is one local commit; `git revert` restores previous behaviour.
Subsystems also switch off individually: `context.code_intelligence.
provider: none`, `memory.enabled: false`, `knowledge.enabled: false`,
`skills.enabled: false`, `caching.enabled: false`.
