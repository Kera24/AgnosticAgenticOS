/* Routing: mode, policies, per-role configuration and the persisted
   decision log explaining why each backend chain was chosen. */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { formatTime } from "../lib/format";
import { BackendChip, Drawer, KV, Panel, Skeleton } from "../components/ui";

type Decision = {
  at: string;
  role: string;
  mode: string;
  chain: string[];
  required_capabilities: Record<string, unknown>;
  rejected: { backend: string; reason: string }[];
  candidates: {
    backend: string;
    strength: number;
    breaker_state: string;
    success_rate: number | null;
    preferred_rank: number;
  }[];
  warnings: string[];
};

type RoutingView = {
  mode: string;
  primary: string | null;
  fallbacks: string[];
  per_agent: Record<string, { primary?: string; fallbacks?: string[] }>;
  agents: Record<string, unknown>;
  policies: Record<string, boolean>;
  decisions: Decision[];
};

export function Routing() {
  const { data, isLoading } = useQuery({
    queryKey: ["routing"],
    queryFn: () => api.get<RoutingView>("/routing"),
  });
  const [selected, setSelected] = useState<Decision | null>(null);

  if (isLoading || !data)
    return (
      <div className="stack">
        <h1 className="visually-hidden">Routing</h1>
        <Panel title="Loading">
          <Skeleton />
        </Panel>
      </div>
    );

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Routing</h1>
        <span className="page-sub">
          how roles map to backends — every chain computation is recorded
          with its reasons
        </span>
      </div>

      <div className="grid cols-2">
        <Panel title="Configuration">
          <KV
            items={[
              ["mode", data.mode],
              [
                "simple chain",
                data.primary
                  ? [data.primary, ...data.fallbacks].join(" → ")
                  : "not configured",
              ],
              [
                "per-agent overrides",
                Object.keys(data.per_agent).length
                  ? Object.entries(data.per_agent)
                      .map(([role, v]) => `${role}→${v.primary}`)
                      .join(", ")
                  : "none",
              ],
              [
                "capability agents",
                Object.keys(data.agents).join(", ") || "none",
              ],
            ]}
          />
        </Panel>
        <Panel title="Policies" sub="auth failures and refusals never fall back">
          <KV
            items={Object.entries(data.policies).map(([key, value]) => [
              key.replaceAll("_", " "),
              value ? "yes" : "no",
            ])}
          />
        </Panel>
      </div>

      <Panel title="Decision log" sub="newest first" flush>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Role</th>
                <th scope="col">Chain</th>
                <th scope="col" className="num">Rejected</th>
                <th scope="col">Warnings</th>
              </tr>
            </thead>
            <tbody>
              {data.decisions.length === 0 && (
                <tr>
                  <td colSpan={5} className="control-hint">
                    no capability-routing decisions recorded yet (mode:{" "}
                    {data.mode})
                  </td>
                </tr>
              )}
              {data.decisions.map((d, i) => (
                <tr
                  key={i}
                  onClick={() => setSelected(d)}
                  style={{ cursor: "pointer" }}
                >
                  <td>{formatTime(d.at)}</td>
                  <td className="mono strong">{d.role}</td>
                  <td>
                    {d.chain.map((b) => (
                      <BackendChip key={b} name={b} />
                    ))}
                  </td>
                  <td className="num">{d.rejected.length}</td>
                  <td className="mono" style={{ fontSize: 12 }}>
                    {d.warnings.join("; ") || "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Drawer
        open={selected !== null}
        title={selected ? `decision · ${selected.role}` : ""}
        onClose={() => setSelected(null)}
      >
        {selected && (
          <div className="stack">
            <KV
              items={[
                ["chain", selected.chain.join(" → ") || "(empty)"],
                [
                  "required capabilities",
                  JSON.stringify(selected.required_capabilities),
                ],
              ]}
            />
            <span className="eyebrow">candidates</span>
            <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
              {selected.candidates.map((c) => (
                <li key={c.backend} className="mono" style={{ fontSize: 12 }}>
                  {c.backend} · strength {c.strength} · {c.breaker_state} ·
                  success{" "}
                  {c.success_rate === null ? "n/a" : c.success_rate}
                </li>
              ))}
            </ul>
            <span className="eyebrow">rejected</span>
            {selected.rejected.length === 0 ? (
              <p className="control-hint">none</p>
            ) : (
              <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
                {selected.rejected.map((r) => (
                  <li key={r.backend} className="mono" style={{ fontSize: 12 }}>
                    {r.backend} — {r.reason}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </Drawer>
    </div>
  );
}
