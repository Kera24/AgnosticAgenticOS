# Backend Authentication

`agentic doctor` and `agentic backends auth` show, per backend:
installed · auth state · smoke test · autonomous readiness.

## Claude (subscription CLI)

Detected via the supported command:

```powershell
claude auth status     # what the OS runs (JSON parsed, text fallback)
claude auth login      # interactive fix when not authenticated
```

States: `authenticated` (method shown when reported, e.g. claude.ai
subscription), `not_authenticated`, `expired`, `conflicting_credentials`
(ANTHROPIC_API_KEY set while subscription OAuth is active — unset it if
you want subscription usage; values are never read or printed),
`executable_missing`, `probe_failed` (older CLI without the command).
No Anthropic API key is required when subscription OAuth is active.

## Qwen — two different things

1. **Qwen Code CLI**: the standalone `qwen auth` command and the OAuth
   free tier are discontinued. Configuration on disk is NOT treated as
   authentication. To use it autonomously:
   - launch `qwen` interactively, use `/auth`, verify with `/doctor`;
   - then `agentic backends smoke qwen` (opt-in — consumes real quota).
   Until that smoke test passes the CLI reports `unverified` and is
   excluded from autonomous routing.
2. **Ollama Qwen models** (`qwen3.5`, …): a separate backend —
   `backend: ollama`, authentication `local_ok`, no Qwen CLI needed.

## Codex & Ollama

Codex keeps its working `codex login status` probe; Ollama is a local
runtime (`local_ok`) with model discovery. `agentic backends smoke
<name>` records an explicit verification for any backend (one real call,
never run by automated tests).
