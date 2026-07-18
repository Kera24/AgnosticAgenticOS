# Managing Application Projects

Agentic OS is installed once and manages application repositories that
live anywhere on your machine. The platform repository is never treated
as your application unless you explicitly adopt it.

## Where things live

| Location | Contains |
|---|---|
| Your application repo (e.g. `C:\Agentic\projects\my-app`) | source, tests, `plan.md`, Docker/Supabase config — your files only |
| `%USERPROFILE%\.agentic-os\` (override: `AGENTIC_HOME`) | `registry.json`, per-project runtime state (`projects\<id>\{project,memory,runs,worktrees,knowledge,logs}`), locks |
| Platform repo (`AgenticOS\.agentic\`) | prompts, schemas, guardrails, builtin skills — OS policy, shared by all projects |

Agentic OS never writes runtime state into your application repository,
and removing a project from the registry never deletes any files.

## Register an existing folder (PowerShell)

```powershell
agentic project add --name "Restaurant Ordering" `
  --root "C:\Agentic\projects\restaurant-ordering" --plan-file plan.md
agentic project init restaurant-ordering     # git/index/memory/vault, no model calls
agentic project doctor restaurant-ordering
agentic project start restaurant-ordering    # architect runs, project enabled
agentic project-run --project restaurant-ordering
```

(Without the `bin\` wrapper on PATH: `py .agentic\run project add …`)

## Create a brand-new project

```powershell
agentic project create --name "My App" --root "C:\Agentic\projects\my-app"
# folder + git + plan template are created and initialised in one step
```

## Everyday operations

```powershell
agentic project list
agentic project show <id>           # status, progress, plan, worktree path
agentic project pause <id>          # scheduler stops picking it up
agentic project resume <id>
agentic project stop <id>           # pause + disable
agentic project open <id>           # open the folder (explicit action)
agentic project archive <id>        # stop managing; files untouched
agentic project remove <id>         # forget the record; files untouched
agentic project relink <id> --root "D:\new\location"   # after moving it
agentic project authorise-root --root "C:\Agentic\projects"  # optional allow-list
```

## Moving between home and office computers

The application repo travels via Git as usual. On the second machine:
`agentic project add` the cloned folder, then `project init`. Runtime
state (memory, index, worktrees) is machine-local by design and rebuilds
on each machine; durable knowledge/architecture lives in the plan and
your repository.

## Existing single-project installs

`agentic project adopt-legacy` registers the platform repository's
implicit project under an id; its state keeps working exactly where it
already is.
