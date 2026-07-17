# Prompt Caching

The broker renders a **byte-stable prefix** (OS policy → role contract →
output schema → project summary) before all per-task content and marks the
boundary. What happens next depends on the backend type:

## Anthropic API backends

The prefix becomes a system block with an explicit `cache_control`
breakpoint. Configure per provider in `providers.<name>`:

```yaml
providers:
  anthropic:
    type: anthropic
    api_key_env: ANTHROPIC_API_KEY
    cache_enabled: true      # default
    cache_ttl: "5m"          # "5m" (API default, not sent) or "1h"
```

Invalid TTLs are never sent. Cache creation/read token counts are recorded
only when the provider reports them (`cache_creation_input_tokens`,
`cache_read_input_tokens`).

## OpenAI-compatible API backends

Exact-prefix caching is automatic server-side; the OS keeps the stable
prefix byte-identical as the system message and sends **no** cache fields.
`usage.prompt_tokens_details.cached_tokens` is read when returned.

## Subscription CLIs (Claude Code, Codex, Qwen)

No caching claim is made. The marker is stripped, the stable prefix
ordering stays consistent (so provider-side caching *may* help), and cache
status remains honestly **unknown**. Cached tokens are never assumed to
extend subscription limits.

## Telemetry

Each context-package summary records tokens by category, omitted tokens,
candidate totals and `estimated_savings_tokens`, all labelled
`measurement: estimated`. Provider-reported usage is stored separately in
the capacity ledger with its `estimated` flag cleared.
