# Page: Capacity (overrides MASTER)

Capacity numbers are trust-critical. The page must make the *confidence* of
every figure impossible to miss.

## Confidence labelling

- Every numeric block carries a confidence chip: `REPORTED` (ok colour),
  `ESTIMATE` (warn colour), `UNKNOWN` (idle colour) — mono uppercase.
- The explanatory line is fixed copy, always visible:
  "Subscription CLI capacity is estimated from local history unless the
  backend reports exact usage or reset information."
- Estimated figures are never shown with more precision than thousands
  (`~48k tokens`), reported figures show exact values.

## Charts

- Token usage per backend: horizontal bars, mono value labels at bar end,
  estimated bars dashed-border fill; a data table alternative toggle.
- Cycle history: compact vertical bars (last 20 cycles), outcome colour +
  tooltip with run id, tokens, duration, result text.
- No streaming/ticking charts: data updates per cycle, so periodic-refresh
  presentation only (per chart-domain guidance: streaming visuals require
  ≥1 Hz data).

## Decision block

The capacity decision (`start | reroute | wait | human_required`) renders as
a decision card: verb in mono display size, reason sentence, selected
backend + fallback candidates as chips, wait-until countdown when waiting.
