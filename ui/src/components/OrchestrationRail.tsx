/* The signature element: a signal-path of the cycle's stages.
   Architect → Conductor → Coder → GATE → QA → Security → Commit.
   The deterministic gate is drawn square + hatched — visibly non-AI.
   Stage states derive from real activity events of the latest cycle. */
import { useMemo, useState } from "react";
import { GitBranch } from "lucide-react";
import type { ActivityEntry, ProjectSnapshot } from "../lib/types";
import { formatTime } from "../lib/format";
import { BackendChip, KV } from "./ui";

export type StageState =
  | "done"
  | "active"
  | "pending"
  | "skipped"
  | "failed"
  | "blocked";

export interface Stage {
  id: string;
  name: string;
  kind: "ai" | "gate" | "commit";
  state: StageState;
  backend?: string | null;
  detail?: string;
  at?: string;
  events: ActivityEntry[];
}

const STAGE_DEFS: { id: string; name: string; kind: Stage["kind"] }[] = [
  { id: "architect", name: "Architect", kind: "ai" },
  { id: "conductor", name: "Conductor", kind: "ai" },
  { id: "coder", name: "Coder", kind: "ai" },
  { id: "gate", name: "Gate", kind: "gate" },
  { id: "qa", name: "QA", kind: "ai" },
  { id: "security", name: "Security", kind: "ai" },
  { id: "commit", name: "Commit", kind: "commit" },
];

/** Derive per-stage state from the activity trail of the most recent cycle. */
export function deriveStages(
  entries: ActivityEntry[],
  project: ProjectSnapshot | undefined,
): Stage[] {
  const running = project?.scheduler.state === "running";
  const currentBackend = project?.scheduler.selected_backend ?? null;
  const projectExists = Boolean(project?.exists);

  // find the slice belonging to the newest cycle (or since last cycle end)
  let lastStart = -1;
  let lastFinish = -1;
  entries.forEach((entry, index) => {
    if (entry.event === "capacity_decision") lastStart = index;
    if (entry.event === "cycle_finished") lastFinish = index;
  });
  const slice =
    lastStart >= 0 ? entries.slice(lastStart) : projectExists ? entries : [];
  const finished = lastFinish >= lastStart && lastFinish >= 0;
  const outcome = finished
    ? (entries[lastFinish]?.outcome as string | undefined)
    : undefined;

  const byStage = new Map<string, ActivityEntry[]>();
  const push = (stage: string, entry: ActivityEntry) => {
    const list = byStage.get(stage) ?? [];
    list.push(entry);
    byStage.set(stage, list);
  };
  let sawGateFail = false;
  let sawQa = false;
  let sawSecurity = false;
  let secVerdict: string | undefined;
  let qaVerdict: string | undefined;
  for (const entry of slice) {
    const event = String(entry.event ?? "");
    if (event === "project_started") push("architect", entry);
    if (event === "capacity_decision") push("conductor", entry);
    if (
      event === "handoff" ||
      event === "fallback" ||
      event === "scope_violation" ||
      event === "backend_error"
    )
      push("coder", entry);
    if (event === "gate_failed") {
      sawGateFail = true;
      push("gate", entry);
    }
    if (event === "qa_review") {
      sawQa = true;
      qaVerdict = entry.verdict as string | undefined;
      push("qa", entry);
    }
    if (event === "security_review") {
      sawSecurity = true;
      secVerdict = entry.verdict as string | undefined;
      push("security", entry);
    }
    if (event === "cycle_finished") push("commit", entry);
  }

  const architectDone = projectExists;
  return STAGE_DEFS.map((def, index) => {
    let state: StageState = "pending";
    const events = byStage.get(def.id) ?? [];
    if (def.id === "architect") {
      state = architectDone ? "done" : "pending";
    } else if (finished) {
      // whole cycle concluded: colour stages by outcome
      if (outcome === "success") {
        state =
          def.id === "security" && !sawSecurity
            ? "skipped"
            : "done";
      } else {
        const failStage = sawQa
          ? qaVerdict !== "pass"
            ? "qa"
            : "security"
          : sawGateFail
            ? "gate"
            : "coder";
        const failIndex = STAGE_DEFS.findIndex((s) => s.id === failStage);
        if (index < failIndex) state = "done";
        else if (index === failIndex)
          state = outcome === "failure" ? "failed" : "blocked";
        else state = "pending";
        if (def.id === "security" && sawSecurity && secVerdict !== "pass")
          state = "blocked";
      }
    } else if (running) {
      // in-flight: infer the active stage from what has been observed
      const order = ["conductor", "coder", "gate", "qa", "security", "commit"];
      let activeId = "conductor";
      if (sawQa) activeId = sawSecurity ? "commit" : "security";
      else if (sawGateFail) activeId = "coder";
      else if (byStage.get("coder")?.length) activeId = "coder";
      else if (byStage.get("conductor")?.length) activeId = "coder";
      const activeIndex = STAGE_DEFS.findIndex((s) => s.id === activeId);
      if (def.id === "architect") state = "done";
      else if (index < activeIndex) state = "done";
      else if (index === activeIndex) state = "active";
      else state = "pending";
      void order;
    }
    return {
      ...def,
      state,
      backend:
        def.kind === "ai"
          ? ((events.at(-1)?.backend as string | undefined) ??
            (def.id === "coder" || def.id === "conductor"
              ? currentBackend
              : null))
          : null,
      at: events.at(-1)?.ts as string | undefined,
      events,
    };
  });
}

export function OrchestrationRail({
  stages,
  running,
}: {
  stages: Stage[];
  running: boolean;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const selectedStage = useMemo(
    () => stages.find((stage) => stage.id === selected) ?? null,
    [stages, selected],
  );
  const summary = stages
    .map((stage) => `${stage.name}: ${stage.state}`)
    .join(", ");
  return (
    <div>
      <div className="orail" role="group" aria-label="Orchestration pipeline">
        <p className="visually-hidden">{summary}</p>
        <div className="orail-track">
          {stages.map((stage, index) => {
            const previous = stages[index - 1];
            const traceClass =
              previous?.state === "done"
                ? stage.state === "active"
                  ? "trace-active"
                  : stage.state === "done" || stage.state === "skipped"
                    ? "trace-done"
                    : ""
                : "";
            return (
              <button
                key={stage.id}
                type="button"
                className={`orail-stage kind-${stage.kind} state-${stage.state} ${traceClass}`}
                aria-pressed={selected === stage.id}
                aria-label={`${stage.name}: ${stage.state}${
                  stage.backend ? `, backend ${stage.backend}` : ""
                }${stage.kind === "gate" ? " (deterministic, non-AI)" : ""}`}
                onClick={() =>
                  setSelected(selected === stage.id ? null : stage.id)
                }
              >
                <span className="orail-node" aria-hidden>
                  {stage.kind === "commit" ? (
                    <GitBranch size={10} />
                  ) : (
                    <span
                      className={`orail-dot${
                        stage.state === "active" && running ? " pulse" : ""
                      }`}
                    />
                  )}
                </span>
                <span className="orail-name">
                  {stage.kind === "gate" ? "GATE" : stage.name}
                </span>
                <span className="orail-sub" aria-hidden>
                  {stage.kind === "ai" && stage.backend && (
                    <BackendChip name={stage.backend} />
                  )}
                  {stage.kind === "gate" && (
                    <span className="orail-meta">non-AI</span>
                  )}
                  {stage.state === "skipped" && (
                    <span className="orail-meta">skipped</span>
                  )}
                </span>
              </button>
            );
          })}
        </div>
      </div>
      {selectedStage && (
        <div className="orail-detail">
          <div style={{ display: "flex", gap: "var(--space-3)", alignItems: "baseline" }}>
            <strong>{selectedStage.name}</strong>
            <span className="eyebrow">{selectedStage.state}</span>
          </div>
          <KV
            items={[
              [
                "Kind",
                selectedStage.kind === "ai"
                  ? "AI agent"
                  : selectedStage.kind === "gate"
                    ? "Deterministic checks (non-AI)"
                    : "Git commit",
              ],
              ["Backend", selectedStage.backend ?? "—"],
              ["Last event", formatTime(selectedStage.at)],
            ]}
          />
          {selectedStage.events.length > 0 ? (
            <div className="log-view" style={{ maxHeight: 180 }}>
              {selectedStage.events
                .slice(-6)
                .map(
                  (event) =>
                    `${event.ts ?? ""}  ${event.event ?? ""}  ${JSON.stringify(
                      Object.fromEntries(
                        Object.entries(event).filter(
                          ([key]) => !["ts", "event", "source"].includes(key),
                        ),
                      ),
                    )}\n`,
                )
                .join("")}
            </div>
          ) : (
            <p className="control-hint">
              No recorded events for this stage in the latest cycle.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
