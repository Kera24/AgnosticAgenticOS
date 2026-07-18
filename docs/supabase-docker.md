# Docker & Supabase per Project

## Docker (restricted adapter)

Each project gets a fixed Compose name `agentic-<project-id>`; every
operation is an allowlisted argv scoped to it — one project can never
touch another's containers.

Allowed: `compose config/build/up/down/ps/logs` and `exec` against
services listed in `docker.approved_exec_services`. Denied always:
`system`/`prune`, privileged containers, docker-socket or host-root
mounts, `-P`/publishing on `0.0.0.0` (bind 127.0.0.1). One machine-wide
build lock serialises builds (`concurrency.maximum_docker_builds`).

## Supabase — local first

Detected per project: `supabase/config.toml`, `migrations/`, `seed.sql`,
`schemas/`, the linked project ref, generated-types location.

**Migration files are the only source of truth.** Any schema change must
exist under `supabase/migrations` — MCP-only or ad-hoc changes are
refused. Safe local loop (all mockable, CLI argv only):

1. `supabase migration new <name>` → edit the SQL
2. full local reset (`supabase db reset --local`) applies migrations +
   `seed.sql`
3. type generation (`supabase gen types typescript --local`)
4. app tests + RLS/security review
5. commit migration + types together

## Remote environments

Policy ladder (`supabase.environments`, overridable):

| action | local | development | staging | production |
|---|---|---|---|---|
| database mutation | automatic | allowed | approval | approval |
| migration apply | automatic | allowed | approval | approval |
| reset / seed | automatic | restricted | **denied** | **denied** |

Remote applies always: confirm the environment, inspect the linked
project, compare migration history, run `db push --dry-run`, save the
evidence to the project's runtime dir, require approval where the policy
says so, apply, verify. Staging/production resets are refused
unconditionally.

## Hosted Supabase MCP

```powershell
agentic mcp add --name supabase-hosted --transport http `
  --url "https://mcp.supabase.com/mcp" --scope project --project <id>
agentic mcp authenticate supabase-hosted   # provider's own OAuth; tokens never stored here
agentic mcp review supabase-hosted
agentic mcp enable supabase-hosted
```

Hosted-MCP tool calls obey the same environment policy and the
migrations-first rule; destructive tools additionally need
`read_only: false` plus an explicit `allowed_tools` entry.
