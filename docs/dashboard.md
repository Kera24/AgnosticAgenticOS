# Dashboard (Control Centre)

```powershell
py .agentic/run ui            # http://127.0.0.1:8765, loopback only
py .agentic/run ui --port 9000 --no-open
```

The CLI remains the source of truth; the dashboard reads the same
persisted state through a local FastAPI service (`.agentic/ui/`) and a
React frontend (`ui/`, build with `npm run build` inside `ui/`).

## Views

Overview (command centre), Projects, Build Control, Agents, Backends,
**Routing** (decision log with reasons), **Context** (index status,
package composition, estimated savings, code search), **Memory**
(search → timeline → details, confirmed forget), **Knowledge** (vault
docs, conflicts, viewer), **Skills** (integrity/risk state, confirmed
enable/disable), Capacity, Verification, Activity, Settings.

## Security model

- binds to 127.0.0.1 only; non-loopback Host headers are rejected;
- mutations with an Origin header require a loopback origin (CSRF);
- no arbitrary command or filesystem endpoint; all paths validated;
- destructive actions (memory forget, skill toggles) require explicit
  confirmation and are written to the audit trail;
- credentials are never displayed; estimates are always labelled
  estimated/reported/unknown; no push/merge/deploy endpoints exist.

## Development

```powershell
Set-Location ui
npm install
npm run build          # served by the Python service from ui/dist
npx vitest run         # component tests
```

`py .agentic/run ui --dev` allows the Vite dev origin
(http://localhost:5173) during frontend development.
