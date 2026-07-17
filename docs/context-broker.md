# Context Broker

The broker (`.agentic/core/context/`) is the only component that assembles
model input. It is deterministic code, never an LLM.

## What it guarantees

- Hard input budget (`context.default_input_budget_tokens`, default 64k)
  minus a protected output reserve; when a backend declares
  `context_window`, the budget shrinks to fit.
- Mandatory sections (policy, role contract, output schema, work order,
  validation failures) are never truncated — an over-budget mandatory set
  **fails the call loudly** instead of sending a corrupted prompt.
- Optional content (code, memory, knowledge, skills) is ranked by
  relevance/freshness/authority, capped by `context.allocation` percents,
  deduplicated (exact text, contained code ranges, superseded memories),
  and truncated only at paragraph/line boundaries.
- Repository text, memories, and skill instructions render inside
  `[UNTRUSTED DATA …]` fences and can never occupy policy sections.
- Every package is summarised (ids, categories, token counts, inclusion/
  omission reasons — no content) in
  `.agentic/memory/context-ledger.jsonl`.

## Token estimates

`tokens ≈ ceil(chars / 4) × safety_multiplier` (default 1.20). Estimates
are always labelled `estimated`; provider tokenizers can be registered via
`core.context.tokenizer.register_tokenizer`.

## Inspecting packages (PowerShell)

```powershell
py .agentic/run context status
py .agentic/run context explain <package-id>
```

Per-role overrides live under `context.roles.<role>` in
`.agentic/config.yaml`.
