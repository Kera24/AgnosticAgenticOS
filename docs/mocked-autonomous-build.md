# Running a Mocked Autonomous Build

The full plan-to-completion flow runs without any provider:

```powershell
py -m pytest tests/test_e2e_autonomy.py -v
```

`test_full_autonomous_build` drives the REAL orchestration (broker,
routing, gate, scheduler, memory, knowledge, index) over scripted backend
adapters through the spec's 21 steps: plan → architect → index → bounded
cycle with a deterministic failure, a repair, a QA rejection, a second
repair and approval → local commit → memory/knowledge/index updates →
persisted cooling → next cycle → final audit → single completion
notification → no automatic restart.

Failure modes live beside it and in the other suites: restarts
(`test_scheduler_project.py`), auth failure without fallback, stale-lock
recovery, context-budget overflow, failing final audit
(`test_e2e_autonomy.py`), corrupt memory DB (`test_memory.py`), corrupt
index (`test_codeintel.py`), unsafe skills (`test_skills.py`), rate-limit
breakers (`test_routing.py`, `test_cli_backends.py`), pause/resume.

## Live smoke tests (opt-in only)

Ordinary tests never contact a provider. To verify a real backend once:

```powershell
$env:AGENTIC_LIVE_SMOKE = "1"
py -m pytest tests/test_live_smoke.py -v
Remove-Item Env:AGENTIC_LIVE_SMOKE
```

This sends one tiny prompt to the configured primary backend and reports
honestly; it consumes real quota, so it stays off by default and in CI.
