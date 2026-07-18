# Windows Setup

Requirements: Python 3.10+ (`py` launcher), Git, and at least one backend
— a subscription CLI (Claude Code / Codex), Ollama, or an API key.

## Install once, start with one command

```powershell
# add the wrapper to PATH once (or call py .agentic\run directly)
$env:Path += ";C:\path\to\AgenticOS\bin"

agentic start            # service + health wait + dashboard in the browser
agentic status           # pid, port, slots, global pause
agentic logs 100         # tail the service log
agentic stop             # graceful shutdown (endpoint), then terminate
```

Launch on login (uses the current localhost experience — no wrapper
needed):

```powershell
$startup = [Environment]::GetFolderPath("Startup")
Copy-Item C:\path\to\AgenticOS\bin\agentic-start.cmd `
  (Join-Path $startup "AgenticOS.cmd")
```

Then register application folders — see `docs/projects.md` and
`docs/fleet.md`.

```powershell
# 1. verify the environment
py .agentic/run doctor

# 2. interactive machine configuration (writes .agentic/config.machine.yaml)
py .agentic/run setup

# 3. start a project
py .agentic/run project-start plan.md
py .agentic/run project-run

# 4. watch it
py .agentic/run ui
```

## Scheduled continuation (optional)

Cooling waits are persisted, never slept. To continue automatically,
register a Task Scheduler job that re-invokes the runner (safe to fire
while cooling — it exits immediately):

```powershell
$action  = New-ScheduledTaskAction -Execute "py" `
  -Argument ".agentic/run project-run" `
  -WorkingDirectory "C:\path\to\your\repo"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName "AgenticOS-cycles" -Action $action `
  -Trigger $trigger
```

Remove with `Unregister-ScheduledTask -TaskName "AgenticOS-cycles"`.

## Notes

- Paths are handled with `os.path`/forward-slash normalisation
  throughout; UNC/OneDrive paths work.
- Ollama: install from ollama.com, `ollama pull qwen3.5` (any generative
  model; embedding models are refused for coding roles).
- Frontend build needs Node 18+: `Set-Location ui; npm install;
  npm run build`.
- Desktop notifications use PowerShell toasts; disable with
  `notifications.desktop: false`.
