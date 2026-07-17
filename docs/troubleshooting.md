# Troubleshooting

Start with `py .agentic/run doctor` — it distinguishes fatal errors,
readiness blockers, warnings, and information, and never claims readiness
when a required component is unusable.

| Symptom | Check |
|---|---|
| `routing.primary is not configured` | run `py .agentic/run setup` or pass `--primary claude` |
| `context broker: mandatory content … exceeds the input budget` | the plan/work order is too large for `context.default_input_budget_tokens`; raise the budget or shrink the input — nothing was sent |
| cycle exits `not_eligible` with cooling | expected; wait, schedule re-invocation, or `py .agentic/run project-run --now` |
| `no usable backend` / `human_required` | `py .agentic/run backends discover` — check installed/auth per backend; auth failures are never routed around |
| backend stuck `authentication_required` | log in with the CLI itself (`claude login`, `codex login`), then `py .agentic/run backends` |
| code intelligence “degraded/fallback” | expected without CCE; `context status` shows the reason; `native` still retrieves |
| stale code index | `py .agentic/run context reindex` |
| memory database corrupt | it was moved to `memory.db.corrupt-<ts>` and recreated automatically; records before the corruption live in the moved file |
| knowledge doc conflict (`*.incoming.md`) | you edited a generated area; merge manually, delete the incoming file |
| skill disabled with `integrity_failure` | files changed since install; reinstall from a pinned checkout (`skills remove` + `skills add --revision`) |
| task blocked `repeated identical failure` | the worker kept producing the same failing diff; inspect `.agentic/runs/cycle-*/checks-*` and the blocker, fix or re-scope, then unblock by editing `backlog.yaml` status to `pending` |
| dashboard 403 | you accessed it from a non-loopback host/origin — by design |
| pytest passes but `run tick` blocks | `verification.commands` empty or auto-detection found nothing; configure explicit commands |

Logs: `.agentic/memory/decisions.jsonl` (audit trail),
`.agentic/runs/<id>/` (per-run artefacts),
`.agentic/memory/notifications.log` (inbox).
