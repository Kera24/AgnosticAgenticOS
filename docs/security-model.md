# Security Model

Enforced in code and locked by repository-wide tests
(`tests/test_invariants.py` plus the per-subsystem suites):

1. **No shell for models.** Model-originated commands must match the
   `execution.safe_commands` allowlist verbatim and run as argv arrays
   (`core/execpolicy.py`). `shell=True` exists only for admin-authored
   configured commands, only inside execpolicy.
2. **Model content cannot change the allowlist** — it is read from
   configuration only.
3. **Workspace confinement.** `gitops.safe_join`, vault paths, skill file
   loads and CCE results all reject path escapes; protected paths queue
   for a human.
4. **Credentials.** Values live only in environment variables; redaction
   runs before every log/prompt/memory/vault write; secret-shaped diffs
   block the cycle; `.env*`/keys are excluded from indexing and packages;
   the dashboard never displays them.
5. **Untrusted content stays data.** Repository text, memories, knowledge
   and skills render inside untrusted fences below OS policy; the broker
   refuses untrusted content in policy sections.
6. **Auth failures and refusals never trigger fallback** — enforced in
   `invoke_backend` and the capability router.
7. **No push/merge/deploy/publish** anywhere in autonomous code (tested
   by source scan). Cycle commits are local; merging is a human act.
8. **Packages are clean.** `run package` ships no runtime ledgers,
   machine config, credentials, indexes or memory databases (tested).
9. **Deletions are explicit.** Memory forget and skill toggles require
   validated targets and confirmation; everything lands in the audit
   trail (`memory/decisions.jsonl`).
10. **Dashboard**: loopback-only bind + Host check + Origin check for
    mutations; no arbitrary command endpoint.
11. **Third-party integrations** (CCE, skills) are pinned, checksummed,
    optional, and local-only; no cloud sync exists.
