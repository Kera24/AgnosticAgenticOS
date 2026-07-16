/* Agents: the specialised roles, their guarantees and routing. Read-only
   roles are visually explicit; the deterministic gate is marked non-AI;
   routing edits go through validated machine-local settings. */
import { useEffect, useState } from "react";
import { Bot, Cpu, Eye, Lock, PenLine, ShieldQuestion } from "lucide-react";
import {
  useAgents,
  useSaveSettings,
  useSettings,
} from "../state/queries";
import { useLive } from "../state/events";
import { formatDuration, formatTime, formatTokens } from "../lib/format";
import { BackendChip, KV, Panel, Skeleton, StatusChip, Working } from "../components/ui";
import type { Agent } from "../lib/types";

function AgentCard({ agent }: { agent: Agent }) {
  return (
    <section className="panel" aria-label={agent.name}>
      <div className="panel-head">
        {agent.ai ? (
          <Bot size={15} aria-hidden />
        ) : (
          <Cpu size={15} aria-hidden />
        )}
        <h3 style={{ font: "var(--type-h2)" }}>{agent.name}</h3>
        {agent.conditional && (
          <span
            className="eyebrow"
            title="Runs only when the change is security-relevant"
          >
            conditional
          </span>
        )}
        <span style={{ marginLeft: "auto" }}>
          {agent.ai ? (
            agent.can_edit ? (
              <StatusChip
                outline
                status={{ tone: "warn", label: "workspace-write" }}
                title="May edit files, only inside the isolated project worktree"
              />
            ) : (
              <StatusChip
                outline
                status={{ tone: "ok", label: "read-only" }}
                title="Cannot edit any file"
              />
            )
          ) : (
            <StatusChip
              outline
              status={{ tone: "idle", label: "non-AI" }}
              title="Deterministic: runs real commands, no model involved"
            />
          )}
        </span>
      </div>
      <div className="panel-body stack" style={{ gap: "var(--space-3)" }}>
        <p style={{ color: "var(--text-secondary)" }}>{agent.purpose}</p>
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          {agent.ai ? (
            agent.can_edit ? (
              <PenLine size={13} aria-hidden />
            ) : (
              <Eye size={13} aria-hidden />
            )
          ) : (
            <Lock size={13} aria-hidden />
          )}
          <span className="control-hint">{agent.permissions}</span>
        </div>
        {agent.ai && (
          <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            <span className="eyebrow">chain</span>
            {agent.chain.length > 0 ? (
              agent.chain.map((backend, i) => (
                <span key={backend} style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                  {i > 0 && <span aria-hidden>→</span>}
                  <BackendChip name={backend} />
                </span>
              ))
            ) : (
              <span className="control-hint">no routing configured</span>
            )}
          </div>
        )}
        <KV
          items={[
            ["Last invocation", formatTime(agent.last_invocation)],
            ["Last backend", agent.last_backend ?? "—"],
            [
              "Last result",
              agent.last_ok === null ? "—" : agent.last_ok ? "ok" : "failed",
            ],
            [
              "Recent",
              `${agent.recent_calls} calls · ${agent.recent_failures} failures`,
            ],
            [
              "Avg duration",
              formatDuration(agent.recent_avg_duration_seconds),
            ],
            [
              "Tokens (recent)",
              `${formatTokens(agent.recent_tokens)}${agent.tokens_estimated ? " (estimate)" : ""}`,
            ],
          ]}
        />
      </div>
    </section>
  );
}

function RoutingEditor() {
  const { data: settings } = useSettings();
  const save = useSaveSettings();
  const { pushToast } = useLive();
  const [mode, setMode] = useState<"simple" | "per_agent">("simple");
  const [primary, setPrimary] = useState("");
  const [fallbacks, setFallbacks] = useState<string[]>([]);
  const [perAgent, setPerAgent] = useState<
    Record<string, { primary: string; fallbacks: string[] }>
  >({});
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (settings && !dirty) {
      setMode(settings.routing.mode);
      setPrimary(settings.routing.primary ?? "");
      setFallbacks(settings.routing.fallbacks);
      setPerAgent(settings.routing.per_agent ?? {});
    }
  }, [settings, dirty]);

  if (!settings) return <Skeleton />;
  const backends = settings.backends_configured;
  const roles = ["architect", "conductor", "coder", "qa", "security"];

  const toggleFallback = (name: string) => {
    setDirty(true);
    setFallbacks((current) =>
      current.includes(name)
        ? current.filter((f) => f !== name)
        : [...current, name],
    );
  };

  const submit = () => {
    const body: Record<string, unknown> = {
      routing: {
        mode,
        primary,
        fallbacks: fallbacks.filter((f) => f !== primary),
        ...(mode === "per_agent"
          ? {
              per_agent: Object.fromEntries(
                roles.map((role) => [
                  role,
                  perAgent[role] ?? { primary, fallbacks },
                ]),
              ),
            }
          : {}),
      },
    };
    save.mutate(body, {
      onSuccess: () => {
        setDirty(false);
        pushToast({
          tone: "ok",
          title: "Routing saved",
          detail: "written to .agentic/config.machine.yaml",
        });
      },
      onError: (error) =>
        pushToast({
          tone: "fail",
          title: "Routing not saved",
          detail: error.message,
          sticky: true,
        }),
    });
  };

  return (
    <div className="stack">
      <div className="field">
        <label htmlFor="routing-mode">Routing mode</label>
        <select
          id="routing-mode"
          className="input"
          value={mode}
          onChange={(e) => {
            setDirty(true);
            setMode(e.target.value as "simple" | "per_agent");
          }}
          style={{ maxWidth: 280 }}
        >
          <option value="simple">simple — one chain for all agents</option>
          <option value="per_agent">per-agent — chain per role</option>
        </select>
      </div>
      <div className="field">
        <label htmlFor="routing-primary">Primary backend</label>
        <select
          id="routing-primary"
          className="input"
          value={primary}
          onChange={(e) => {
            setDirty(true);
            setPrimary(e.target.value);
          }}
          style={{ maxWidth: 280 }}
        >
          <option value="" disabled>
            choose a backend
          </option>
          {backends.map((backend) => (
            <option key={backend} value={backend}>
              {backend}
            </option>
          ))}
        </select>
      </div>
      <fieldset style={{ border: "none", padding: 0, margin: 0 }}>
        <legend className="eyebrow" style={{ marginBottom: "var(--space-2)" }}>
          ordered fallbacks
        </legend>
        <div style={{ display: "flex", gap: "var(--space-3)", flexWrap: "wrap" }}>
          {backends
            .filter((backend) => backend !== primary)
            .map((backend) => (
              <label key={backend} className="check-row">
                <input
                  type="checkbox"
                  checked={fallbacks.includes(backend)}
                  onChange={() => toggleFallback(backend)}
                />
                <BackendChip name={backend} />
                {fallbacks.includes(backend) && (
                  <span className="control-hint">
                    #{fallbacks.indexOf(backend) + 1}
                  </span>
                )}
              </label>
            ))}
        </div>
      </fieldset>
      {mode === "per_agent" && (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Agent</th>
                <th scope="col">Primary</th>
              </tr>
            </thead>
            <tbody>
              {roles.map((role) => (
                <tr key={role}>
                  <td className="mono strong">{role}</td>
                  <td>
                    <select
                      className="input"
                      aria-label={`${role} primary backend`}
                      value={perAgent[role]?.primary ?? primary}
                      onChange={(e) => {
                        setDirty(true);
                        setPerAgent((current) => ({
                          ...current,
                          [role]: {
                            primary: e.target.value,
                            fallbacks,
                          },
                        }));
                      }}
                      style={{ maxWidth: 220 }}
                    >
                      {backends.map((backend) => (
                        <option key={backend} value={backend}>
                          {backend}
                        </option>
                      ))}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
        <button
          className="btn primary"
          disabled={!primary || save.isPending || !dirty}
          title={
            !primary
              ? "choose a primary backend"
              : !dirty
                ? "no changes to save"
                : undefined
          }
          onClick={submit}
        >
          {save.isPending ? <Working label="Saving…" /> : "Save routing"}
        </button>
        <span className="control-hint">
          Saved to machine-local configuration only. Authentication stays
          with each CLI — tokens are never shown or stored here.
        </span>
      </div>
    </div>
  );
}

export function Agents() {
  const { data, isLoading } = useAgents();
  if (isLoading)
    return (
      <div className="stack">
        <h1 className="visually-hidden">Agents</h1>
        <Panel title="Loading">
          <Skeleton />
        </Panel>
      </div>
    );
  const agents = data?.agents ?? [];
  return (
    <div className="stack">
      <div className="page-head">
        <h1>Agents</h1>
        <span className="page-sub">
          no agent approves its own work; the gate is not an AI
        </span>
      </div>
      <div className="grid cols-3">
        {agents.map((agent) => (
          <AgentCard key={agent.id} agent={agent} />
        ))}
      </div>
      <Panel
        title="Routing"
        sub="which backend serves each agent, with ordered fallbacks"
      >
        <RoutingEditor />
      </Panel>
      <Panel title="Guarantees">
        <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "flex-start" }}>
          <ShieldQuestion size={15} aria-hidden style={{ flex: "none", marginTop: 2 }} />
          <p className="control-hint">
            Architect, Conductor, QA and Security run read-only. Only the
            Coder may write, and only inside the isolated project worktree.
            The deterministic gate runs your repository's real checks and no
            model can override its verdict. Security review triggers
            conditionally on risk signals. Fallback never bypasses an
            authentication failure or a safety refusal.
          </p>
        </div>
      </Panel>
    </div>
  );
}
