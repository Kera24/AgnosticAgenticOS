# MCP Servers

The gateway (`core/mcp.py`) manages Model Context Protocol servers
provider-neutrally: native pass-through for MCP-capable CLIs (a
task-local `.mcp.json` naming only the granted servers/tools) and
OS-mediated invocation for everything else.

```powershell
agentic mcp add --name supabase-local --transport stdio `
  --command "npx -y @supabase/mcp-server-supabase" `
  --scope project --project restaurant-ordering
agentic mcp review supabase-local     # after you inspect it
agentic mcp enable supabase-local
agentic mcp test supabase-local       # handshake + tools/list
agentic mcp tools supabase-local      # with per-tool policy verdicts
```

Rules enforced in code:

- servers are untrusted until reviewed; disabled servers cannot be called;
- project-scoped servers serve only their project;
- tasks receive a narrowed tool grant — never the whole catalog;
- destructive-looking tools (`delete/drop/reset/push/apply_migration/…`)
  require `read_only: false` AND an explicit `allowed_tools` entry;
- outputs are timeout-bounded, token-capped (`maximum_output_tokens`),
  secret-redacted, audit-logged (`~/.agentic-os/mcp-calls.jsonl`) and
  fenced as UNTRUSTED data by the Context Broker — tool output can never
  change OS policy;
- records hold no credentials; OAuth belongs to the server's own tooling
  and only its status is tracked (`authenticate` guides, `test`
  verifies).

Transports: `stdio` (newline JSON-RPC over an argv process — never a
shell), `http` (JSON-RPC POST through the shared provider transport),
`sse` registered for compatibility.
