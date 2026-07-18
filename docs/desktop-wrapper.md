# Desktop Wrapper Architecture (future — not built in this release)

The localhost dashboard remains the primary interface. This document
defines the boundary a future desktop wrapper snaps onto WITHOUT any
change to orchestration code, and records the framework recommendation.

## The boundary that already exists

| Wrapper need | Provided by |
|---|---|
| Service lifecycle | `agentic start/stop/restart/status` (`core/service.py`) — injectable spawn/health/terminate; single-instance safe |
| Liveness / readiness | `GET /api/v1/health`, `GET /api/v1/readiness` |
| Update status | `GET /api/v1/version` (UI version, platform git revision, config version) |
| Graceful shutdown | `POST /api/v1/shutdown` (loopback + Origin guarded, confirmation required) |
| Deep links / open project | `GET /api/v1/portfolio` exposes absolute `root_path`, knowledge vault and worktree paths — the wrapper opens them with native shell APIs |
| Notifications | file inbox `<memory>/notifications.log` + SSE `/api/v1/events`; the wrapper mirrors these as native toasts |
| Frontend | `ui/dist` static bundle, servable by the Python service or loadable by the wrapper pointing at `http://127.0.0.1:<port>` |

The frontend has no dependency on how the server was started; the service
binds loopback only and enforces Host/Origin checks, so a wrapper webview
pointing at 127.0.0.1 needs no CORS changes.

## Wrapper responsibilities (and hard limits)

The wrapper: starts and monitors the local service (sidecar), shows the
existing dashboard, minimises to tray, sends native notifications, opens
project folders and Obsidian vaults, and surfaces update status.

The wrapper NEVER: contains orchestration logic, executes model or shell
commands, exposes the service beyond loopback, or loads remote content
with native privileges (webview navigation pinned to 127.0.0.1).

## Tauri vs Electron

| Criterion | Tauri 2 | Electron |
|---|---|---|
| Frontend reuse | as-is (any web bundle) | as-is |
| Windows installer size | ~5–15 MB (system WebView2) | ~80–150 MB (bundled Chromium) |
| Memory footprint | low (shared WebView2) | high |
| Security model | Rust host, deny-by-default IPC allowlist, no Node in renderer | powerful but historically footgun-prone (contextIsolation etc.) |
| Sidecar process management | first-class (`shell` sidecar API, scoped) | manual via child_process |
| Auto-update | built-in updater (signed) | electron-updater (mature) |
| Tray / notifications | built-in | built-in |
| Team familiarity | new (Rust host code is minimal glue) | JS-familiar |
| Fit with "wrapper must hold no logic" | excellent — IPC allowlist makes adding logic hard by default | requires discipline |

**Recommendation: Tauri 2.** The wrapper is deliberately logic-free, so
Electron's main advantage (rich Node host code) is an anti-feature here.
Tauri gives a ~10× smaller Windows installer on WebView2, a
deny-by-default IPC surface that structurally enforces the "no
orchestration in the wrapper" rule, scoped sidecar management for
`agentic start`-equivalent supervision, and a signed updater. The only
Rust needed is generated scaffolding plus a few dozen lines of sidecar
and tray glue.

## Launch-on-login (available today, no wrapper needed)

```powershell
# per-user startup shortcut for the current localhost experience
$startup = [Environment]::GetFolderPath("Startup")
Copy-Item bin\agentic-start.cmd (Join-Path $startup "AgenticOS.cmd")
```

System-tray support is deferred to the wrapper: adding it to the Python
service would couple orchestration to a GUI toolkit, which this
architecture forbids.
