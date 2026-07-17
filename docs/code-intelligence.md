# Code Intelligence

Retrieval-quality code context behind one adapter interface
(`.agentic/core/codeintel/`). Configure under
`context.code_intelligence`:

| provider | what it is |
|---|---|
| `none` | snapshot-only behaviour; always available |
| `native` | dependency-free term-scored search over tracked files (default) |
| `cce` | external Code Context Engine CLI, detected + version-validated |

The chain `provider → fallback → none` degrades honestly; the active
adapter and the fallback reason appear in `doctor` and `context status`.

## CCE adapter rules

- never vendored or imported; only an installed CLI is invoked (argv, no
  shell), with timeouts and malformed-JSON handling;
- all result paths must stay inside the workspace or are discarded;
- `.git`, `.agentic` runtime, `.env*`, keys, dependencies and build output
  are excluded from indexing and filtered from results;
- a non-local `cce_endpoint` is refused — code never leaves the machine.

## Lifecycle

Full index after `project-start`; incremental index after each successful
cycle commit; `context reindex` on demand. Index revision persists in
`.agentic/memory/code-index/state.json`; a moved HEAD marks it stale.

```powershell
py .agentic/run context status
py .agentic/run context search "discount calculation"
py .agentic/run context reindex
```

The retrieval-vs-full benchmark in `tests/test_codeintel.py` is a **local
measurement on synthetic fixtures**, not a general claim.
