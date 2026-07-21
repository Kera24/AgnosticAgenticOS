/* Capability: per-project Capability Plan, Capability Graph satisfaction
   breakdown, the pending setup-actions autonomy inbox, the Completion
   Contract, and the platform-wide Model Capability Registry / frontier
   capacity. Read-only -- every mutation still happens through its own
   existing page (Skills, Mcp, Build). */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { KV, Panel, Skeleton, StatusChip } from "../components/ui";

type PortfolioProject = { id: string; name: string };

type CapabilitySnapshot = {
  project_id: string;
  plan_summary: {
    confidence: number;
    required_capabilities: string[];
    optional_capabilities: string[];
    protected_actions: string[];
    unresolved_questions: { section: string; question: string }[];
  } | null;
  graph_summary: {
    capability_count: number;
    by_state: Record<string, number>;
    unresolved: string[];
  } | null;
  setup_actions: { id: string; kind: string; reason: string }[];
  completion_contract: {
    complete: boolean;
    verified_count: number;
    total_count: number;
    unverified: string[];
  } | null;
};

type ModelRecord = {
  backend: string;
  model_id: string;
  reasoning_class: string;
  available: boolean;
  circuit_breaker: string;
};

type OrchestrationSnapshot = {
  generated_at: string | null;
  records: ModelRecord[];
  frontier_capacity: { status: string; worker_share: number;
    reserve_percent: number } | null;
};

const STATE_TONE: Record<string, "ok" | "warn" | "fail" | "idle"> = {
  satisfied: "ok", available: "ok", waived: "ok",
  partially_satisfied: "warn", resolving: "warn", unresolved: "idle",
  blocked: "fail",
};

export function Capability() {
  const { data: portfolio } = useQuery({
    queryKey: ["portfolio"],
    queryFn: () => api.get<{ projects: PortfolioProject[] }>("/portfolio"),
  });
  const [projectId, setProjectId] = useState<string>("");
  const activeId = projectId || portfolio?.projects[0]?.id || "";

  const { data: snap, isLoading } = useQuery({
    queryKey: ["capability", activeId],
    queryFn: () => api.get<CapabilitySnapshot>(
      `/portfolio/${activeId}/capability`),
    enabled: !!activeId,
  });
  const { data: orchestration } = useQuery({
    queryKey: ["orchestration"],
    queryFn: () => api.get<OrchestrationSnapshot>("/orchestration"),
  });

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Capability</h1>
        <span className="page-sub">
          Capability Plan, resolution status, and completion evidence —
          read-only.
        </span>
      </div>

      <Panel title="Project">
        {!portfolio ? (
          <Skeleton />
        ) : portfolio.projects.length === 0 ? (
          <p className="control-hint">
            No registered projects yet — add one in Portfolio.
          </p>
        ) : (
          <select
            className="input"
            value={activeId}
            onChange={(e) => setProjectId(e.target.value)}
          >
            {portfolio.projects.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        )}
      </Panel>

      {isLoading || !snap ? (
        <Panel title="Loading"><Skeleton /></Panel>
      ) : (
        <div className="grid cols-2">
          <Panel title="Capability Plan">
            {!snap.plan_summary ? (
              <p className="control-hint">
                No Capability Plan yet — run `capability plan` for this
                project.
              </p>
            ) : (
              <KV
                items={[
                  ["confidence", String(snap.plan_summary.confidence)],
                  ["required", snap.plan_summary.required_capabilities
                    .join(", ") || "—"],
                  ["optional", snap.plan_summary.optional_capabilities
                    .join(", ") || "—"],
                  ["protected actions", snap.plan_summary.protected_actions
                    .join(", ") || "—"],
                  ["unresolved questions",
                    String(snap.plan_summary.unresolved_questions.length)],
                ]}
              />
            )}
          </Panel>

          <Panel title="Capability Graph">
            {!snap.graph_summary ? (
              <p className="control-hint">No Capability Graph built yet.</p>
            ) : (
              <div className="stack" style={{ gap: "var(--space-2)" }}>
                <p>{snap.graph_summary.capability_count} capabilities</p>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  {Object.entries(snap.graph_summary.by_state).map(
                    ([state, count]) => (
                      <StatusChip
                        key={state}
                        outline
                        status={{
                          tone: STATE_TONE[state] ?? "idle",
                          label: `${state}: ${count}`,
                        }}
                      />
                    ),
                  )}
                </div>
              </div>
            )}
          </Panel>

          <Panel title="Setup actions" sub="autonomy inbox">
            {snap.setup_actions.length === 0 ? (
              <p className="control-hint">Nothing pending.</p>
            ) : (
              <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
                {snap.setup_actions.map((a) => (
                  <li key={a.id} className="mono">{a.kind}: {a.reason}</li>
                ))}
              </ul>
            )}
          </Panel>

          <Panel title="Completion Contract">
            {!snap.completion_contract ? (
              <p className="control-hint">No final audit run yet.</p>
            ) : (
              <div className="stack" style={{ gap: "var(--space-2)" }}>
                <StatusChip
                  status={snap.completion_contract.complete
                    ? { tone: "ok", label: "All requirements evidenced" }
                    : { tone: "warn", label: "Requirements unverified" }}
                />
                <p>
                  {snap.completion_contract.verified_count} /{" "}
                  {snap.completion_contract.total_count} requirements
                  verified
                </p>
                {snap.completion_contract.unverified.length > 0 && (
                  <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
                    {snap.completion_contract.unverified.map((r) => (
                      <li key={r} className="control-hint">{r}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </Panel>
        </div>
      )}

      <Panel title="Model orchestration" sub="frontier capacity + registry">
        {!orchestration ? (
          <Skeleton />
        ) : orchestration.records.length === 0 ? (
          <p className="control-hint">
            No Model Capability Registry snapshot yet — run `models refresh`.
          </p>
        ) : (
          <div className="stack" style={{ gap: "var(--space-3)" }}>
            {orchestration.frontier_capacity && (
              <StatusChip
                status={orchestration.frontier_capacity.status === "ok"
                  ? { tone: "ok", label: "Frontier capacity ok" }
                  : { tone: "warn", label: "Frontier reserve exhausted" }}
              />
            )}
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th scope="col">Backend</th>
                    <th scope="col">Model</th>
                    <th scope="col">Class</th>
                    <th scope="col">Available</th>
                    <th scope="col">Breaker</th>
                  </tr>
                </thead>
                <tbody>
                  {orchestration.records.map((r) => (
                    <tr key={r.backend + r.model_id}>
                      <td className="mono strong">{r.backend}</td>
                      <td className="mono">{r.model_id}</td>
                      <td>{r.reasoning_class}</td>
                      <td>{r.available ? "yes" : "no"}</td>
                      <td className="mono">{r.circuit_breaker}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </Panel>
    </div>
  );
}
