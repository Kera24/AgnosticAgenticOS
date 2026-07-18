/* MCP: registered servers, scope, auth state, health, tool policy.
   Enable/disable are confirmed; secrets never appear. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../lib/api";
import { ConfirmDialog, Drawer, KV, Panel, Skeleton, StatusChip }
  from "../components/ui";

type Server = {
  id: string;
  name: string;
  transport: string;
  command: string | null;
  args: string[];
  url: string | null;
  scope: string;
  project_id: string | null;
  environment: string;
  read_only: boolean;
  authentication_type: string;
  authentication_status: string;
  allowed_tools: string[];
  denied_tools: string[];
  maximum_output_tokens: number;
  timeout: number;
  enabled: boolean;
  reviewed: boolean;
  risk_level: string;
  last_health_check: string | null;
};

export function Mcp() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["mcp"],
    queryFn: () => api.get<{ servers: Server[] }>("/mcp"),
  });
  const [selected, setSelected] = useState<Server | null>(null);
  const [confirm, setConfirm] = useState<{
    server: Server; action: "enable" | "disable";
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);

  const act = useMutation({
    mutationFn: ({ id, action, confirmed }:
      { id: string; action: string; confirmed?: boolean }) =>
      api.post(`/mcp/${id}/${action}`,
               confirmed ? { confirm: true } : {}),
    onSuccess: (result, vars) => {
      setConfirm(null);
      setError(null);
      if (vars.action === "test")
        setTestResult(JSON.stringify(result, null, 2));
      queryClient.invalidateQueries({ queryKey: ["mcp"] });
    },
    onError: (err) => {
      setConfirm(null);
      setError(err instanceof ApiError ? err.message : String(err));
    },
  });

  if (isLoading || !data)
    return (
      <div className="stack">
        <h1 className="visually-hidden">MCP servers</h1>
        <Panel title="Loading"><Skeleton /></Panel>
      </div>
    );

  return (
    <div className="stack">
      <div className="page-head">
        <h1>MCP Servers</h1>
        <span className="page-sub">
          tool servers are untrusted until reviewed; outputs are capped,
          redacted and fenced as data. Register via CLI:{" "}
          <code className="mono">agentic mcp add --name … --command …</code>
        </span>
      </div>

      {error && (
        <Panel title="Action failed">
          <p className="control-hint" role="alert">{error}</p>
        </Panel>
      )}

      <Panel title="Registered servers" flush>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Server</th>
                <th scope="col">Transport</th>
                <th scope="col">Scope</th>
                <th scope="col">Auth</th>
                <th scope="col">Mode</th>
                <th scope="col">State</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.servers.length === 0 && (
                <tr>
                  <td colSpan={7} className="control-hint">
                    no MCP servers registered
                  </td>
                </tr>
              )}
              {data.servers.map((s) => (
                <tr key={s.id}>
                  <td>
                    <button className="btn" style={{ minHeight: 24 }}
                            onClick={() => setSelected(s)}>
                      <span className="mono strong">{s.id}</span>
                    </button>
                  </td>
                  <td className="mono">{s.transport}</td>
                  <td className="mono">
                    {s.scope}
                    {s.project_id ? `:${s.project_id}` : ""}
                  </td>
                  <td>
                    <StatusChip outline status={{
                      tone: s.authentication_status === "ok" ? "ok"
                        : s.authentication_status === "error" ? "fail"
                        : "idle",
                      label: s.authentication_type === "none"
                        ? "none needed" : s.authentication_status,
                    }} />
                  </td>
                  <td>{s.read_only ? "read-only" : "writable"}</td>
                  <td>
                    <StatusChip outline status={
                      s.enabled ? { tone: "ok", label: "enabled" }
                      : s.reviewed ? { tone: "idle", label: "disabled" }
                      : { tone: "warn", label: "unreviewed" }} />
                  </td>
                  <td>
                    <span style={{ display: "flex", gap: 4 }}>
                      <button className="btn" style={{ minHeight: 24 }}
                        onClick={() =>
                          act.mutate({ id: s.id, action: "test" })}>
                        test
                      </button>
                      {!s.reviewed && (
                        <button className="btn" style={{ minHeight: 24 }}
                          onClick={() =>
                            act.mutate({ id: s.id, action: "review" })}>
                          mark reviewed
                        </button>
                      )}
                      <button className="btn" style={{ minHeight: 24 }}
                        onClick={() =>
                          setConfirm({ server: s,
                                       action: s.enabled ? "disable"
                                                         : "enable" })}>
                        {s.enabled ? "disable…" : "enable…"}
                      </button>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      {testResult && (
        <Panel title="Last health check"
               actions={<button className="btn"
                                onClick={() => setTestResult(null)}>
                          clear
                        </button>}>
          <pre className="code-block" style={{ whiteSpace: "pre-wrap" }}>
            {testResult}
          </pre>
        </Panel>
      )}

      <Drawer open={selected !== null} title={selected?.name ?? ""}
              onClose={() => setSelected(null)}>
        {selected && (
          <KV
            items={[
              ["id", selected.id],
              ["transport", selected.transport],
              ["command",
               selected.command
                 ? `${selected.command} ${selected.args.join(" ")}`
                 : (selected.url ?? "—")],
              ["scope",
               selected.scope +
                 (selected.project_id ? ` (${selected.project_id})` : "")],
              ["environment", selected.environment],
              ["mode", selected.read_only ? "read-only" : "writable"],
              ["allowed tools",
               selected.allowed_tools.join(", ") || "(none granted)"],
              ["denied tools",
               selected.denied_tools.join(", ") || "—"],
              ["output cap", `${selected.maximum_output_tokens} tokens`],
              ["timeout", `${selected.timeout}s`],
              ["risk", selected.risk_level],
              ["last health check", selected.last_health_check ?? "never"],
            ]}
          />
        )}
      </Drawer>

      <ConfirmDialog
        open={confirm !== null}
        title={confirm?.action === "enable" ? "Enable MCP server"
                                            : "Disable MCP server"}
        body={
          <p>
            {confirm?.action === "enable" ? (
              <>Enable <code>{confirm?.server.id}</code>? Its granted
              tools become callable through the gateway (outputs stay
              capped, redacted and untrusted).</>
            ) : (
              <>Disable <code>{confirm?.server.id}</code>? No tool on it
              can be called until re-enabled.</>
            )}
          </p>
        }
        confirmLabel={confirm?.action ?? ""}
        danger={confirm?.action === "disable"}
        working={act.isPending}
        onConfirm={() =>
          confirm && act.mutate({ id: confirm.server.id,
                                  action: confirm.action,
                                  confirmed: true })}
        onCancel={() => setConfirm(null)}
      />
    </div>
  );
}
