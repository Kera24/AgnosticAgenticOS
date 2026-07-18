# Running Multiple Projects (Fleet)

```powershell
agentic start                 # service + dashboard
agentic project list
agentic fleet tick            # one scheduling pass (or let a timer run it)
agentic fleet status
agentic fleet pause | resume  # global emergency pause
```

Up to `concurrency.maximum_active_projects` (default 4) run per tick;
model, per-backend, docker-build, heavy-local and test-job slots are
independent pools (`concurrency:` in `.agentic/config.yaml`). A cooling
or waiting project never blocks another.

## Project states

`uninitialised → ready → queued → preparing/running/testing/reviewing/
repairing → cooling → …` plus `paused`, `blocked`, `completed`, `failed`,
`archived`. The dashboard's Portfolio page shows the exact waiting reason
for every project: waiting for a Claude/docker/test slot, cooling until a
timestamp, insufficient estimated capacity (labelled
reported/estimated/unknown), lease held by another process, waiting for
external approval, or blocked by a failing migration.

## Fairness

Priority first (`project add --priority N`, higher wins), then
least-recently-scheduled rotation so nothing starves. Every decision is
persisted to `~/.agentic-os/fleet-decisions.jsonl`.

## Recovery

Slot allocations carry pid + expiry; a crashed process's slots are reaped
on the next tick, leases expire, and `agentic start` resumes only
eligible projects. Nothing runs twice: a project holding a slot or lease
is never started again.

## Scheduled ticks (PowerShell)

```powershell
$action = New-ScheduledTaskAction -Execute "py" `
  -Argument ".agentic/run fleet tick" -WorkingDirectory "C:\path\to\AgenticOS"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName "AgenticOS-fleet" -Action $action -Trigger $trigger
```
