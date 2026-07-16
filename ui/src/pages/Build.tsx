/* Build Control: the project's control surface. Controls are disabled with
   an explanation whenever the action is invalid; safe actions need no
   confirmation; server-side single-flight prevents duplicates. */
import { useState } from "react";
import {
  CheckCheck,
  Pause,
  Play,
  RefreshCw,
  StepForward,
} from "lucide-react";
import {
  useActivity,
  useCapacity,
  useOperations,
  useProject,
  useProjectAction,
} from "../state/queries";
import { useLive } from "../state/events";
import { decisionStatus, schedulerStatus } from "../lib/status";
import { formatTime, formatTokens } from "../lib/format";
import {
  BackendChip,
  Countdown,
  KV,
  Panel,
  Skeleton,
  StatusChip,
  Working,
} from "../components/ui";

export function Build() {
  const { data: project, isLoading, refetch, isFetching } = useProject();
  const { data: capacity } = useCapacity();
  const { data: opsData } = useOperations();
  const { data: activity } = useActivity(200);
  const { pushToast } = useLive();
  const run = useProjectAction("run");
  const pause = useProjectAction("pause");
  const resume = useProjectAction("resume");
  const review = useProjectAction("review");
  const [expanded, setExpanded] = useState(false);

  if (isLoading)
    return (
      <div className="stack">
        <h1 className="visually-hidden">Build Control</h1>
        <Panel title="Loading">
          <Skeleton />
        </Panel>
      </div>
    );

  const scheduler = project?.scheduler;
  const state = scheduler?.state;
  const exists = Boolean(project?.exists);
  const runningOp = (opsData?.operations ?? []).find(
    (op) => op.status === "running" && op.group === "project",
  );
  const busy = Boolean(runningOp) || state === "running";

  const why = {
    start: !exists
      ? "No project started — create one in Projects."
      : busy
        ? "A cycle or project operation is already running."
        : state === "paused"
          ? "Project is paused — resume first."
          : state === "complete"
            ? "Project is complete."
            : project?.eligible === false
              ? (project.eligible_reason ?? "not eligible")
              : null,
    pause:
      !exists || state === "paused" || state === "complete"
        ? "Only an active project can be paused."
        : null,
    resume: state !== "paused" ? "The project is not paused." : null,
    audit: !exists
      ? "No project started."
      : busy
        ? "A project operation is already running."
        : null,
  };

  const onError = (title: string) => (error: Error) =>
    pushToast({ tone: "fail", title, detail: error.message, sticky: true });

  const decision = capacity?.decision;
  const lastHandoff = [...(activity?.entries ?? [])]
    .reverse()
    .find((entry) => entry.event === "handoff" || entry.event === "fallback");
  const lastRepair = [...(activity?.entries ?? [])]
    .reverse()
    .find(
      (entry) =>
        entry.event === "gate_failed" || entry.event === "scope_violation",
    );

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Build Control</h1>
        <span className="page-sub">
          start, pause and audit — never push, merge or deploy
        </span>
      </div>

      <Panel title="Controls">
        <div
          style={{
            display: "flex",
            gap: "var(--space-3)",
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <button
            className="btn primary big"
            disabled={Boolean(why.start) || run.isPending}
            title={why.start ?? "runs the next eligible development cycle"}
            onClick={() =>
              run.mutate(undefined, { onError: onError("Start cycle failed") })
            }
          >
            {run.isPending || runningOp?.kind === "project.run" ? (
              <Working label="Cycle running…" />
            ) : (
              <>
                <StepForward size={16} aria-hidden /> Start next cycle
              </>
            )}
          </button>
          <button
            className="btn big"
            disabled={Boolean(why.resume) || resume.isPending}
            title={why.resume ?? "clears the pause and allows cycles again"}
            onClick={() =>
              resume.mutate(undefined, { onError: onError("Resume failed") })
            }
          >
            <Play size={15} aria-hidden /> Resume
          </button>
          <button
            className="btn big"
            disabled={Boolean(why.pause) || pause.isPending}
            title={why.pause ?? "no new cycles until you resume"}
            onClick={() =>
              pause.mutate(undefined, { onError: onError("Pause failed") })
            }
          >
            <Pause size={15} aria-hidden /> Pause
          </button>
          <button
            className="btn big"
            disabled={Boolean(why.audit) || review.isPending}
            title={
              why.audit ??
              "runs the evidence-based final audit (uses one QA review)"
            }
            onClick={() =>
              review.mutate(undefined, { onError: onError("Audit failed") })
            }
          >
            {runningOp?.kind === "project.review" ? (
              <Working label="Auditing…" />
            ) : (
              <>
                <CheckCheck size={15} aria-hidden /> Run final audit
              </>
            )}
          </button>
          <button
            className="btn big"
            onClick={() => refetch()}
            title="re-read project state from disk"
          >
            <RefreshCw
              size={15}
              aria-hidden
              className={isFetching ? "spin" : undefined}
            />{" "}
            Refresh
          </button>
        </div>
        {(why.start && exists && state !== "paused") && (
          <p className="control-hint" style={{ marginTop: "var(--space-3)" }} role="status">
            Start is unavailable: {why.start}
          </p>
        )}
        <p className="control-hint" style={{ marginTop: "var(--space-2)" }}>
          Duplicate cycles are prevented by the project lock; a running cycle
          finishes on its own. Pausing takes effect before the next cycle.
        </p>
      </Panel>

      <div className="grid cols-2">
        <Panel title="State">
          <KV
            items={[
              [
                "Scheduler",
                <StatusChip
                  key="s"
                  status={schedulerStatus(state, scheduler?.project_status)}
                  pulse={state === "running"}
                />,
              ],
              [
                "Cooling",
                state === "cooling" ? (
                  <span
                    key="c"
                    style={{ display: "inline-flex", gap: 8, alignItems: "center" }}
                  >
                    <Countdown
                      until={scheduler?.next_run_at}
                      label="Cooling countdown"
                    />
                    <span className="control-hint">
                      {scheduler?.cooling_reason} · until{" "}
                      {formatTime(scheduler?.next_run_at)}
                    </span>
                  </span>
                ) : (
                  "not cooling"
                ),
              ],
              ["Eligible", project?.eligible ? "yes" : (project?.eligible_reason ?? "—")],
              [
                "Selected task",
                project?.next_task ? (
                  <span key="t">
                    <span className="mono">{project.next_task.id}</span> —{" "}
                    {project.next_task.description}
                  </span>
                ) : (
                  "backlog empty or blocked"
                ),
              ],
              ["Branch", <span key="b" className="mono">{project?.branch}</span>],
              [
                "Worktree",
                <span key="w" className="mono">
                  {project?.worktree_exists ? project.worktree : "not created yet"}
                </span>,
              ],
              [
                "Operation",
                runningOp
                  ? `${runningOp.kind} since ${formatTime(runningOp.started_at)}`
                  : "none running",
              ],
            ]}
          />
        </Panel>

        <Panel title="Capacity decision">
          {decision ? (
            <div className={`decision-card tone-${decisionStatus(decision.decision).tone}`}>
              <span className="decision-verb">
                {decision.decision.replace("_", " ")}
              </span>
              <p>{decision.reason}</p>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                <span className={`confidence-chip ${decision.confidence}`}>
                  {decision.confidence}
                </span>
                {decision.selected_backend && (
                  <BackendChip name={decision.selected_backend} />
                )}
                {decision.fallback_candidates.map((backend) => (
                  <span key={backend} className="control-hint">
                    → <BackendChip name={backend} />
                  </span>
                ))}
              </div>
              <span className="control-hint">
                requires {formatTokens(decision.required_estimated_tokens)}{" "}
                tokens (estimate, not a provider quota)
              </span>
              {decision.wait_until && (
                <Countdown until={decision.wait_until} label="Capacity wait" />
              )}
            </div>
          ) : (
            <p className="control-hint">
              No routing configured yet — set a primary backend in Settings
              or run <span className="mono">py .agentic/run setup</span>.
            </p>
          )}
        </Panel>
      </div>

      <Panel
        title="Blockers"
        sub={
          project?.human_blockers.length
            ? `${project.human_blockers.length} need a human decision`
            : undefined
        }
      >
        {project && project.blockers.length > 0 ? (
          <div className="stack" style={{ gap: "var(--space-2)" }}>
            {project.blockers.map((blocker, i) => (
              <div
                key={i}
                className={`blocker-card${blocker.human_only ? " human" : ""}`}
              >
                <span className="eyebrow">
                  {blocker.human_only
                    ? "human decision required"
                    : `task ${blocker.task ?? "—"}`}{" "}
                  · {formatTime(blocker.created_at)}
                </span>
                <span className="blocker-reason">{blocker.reason}</span>
              </div>
            ))}
            <p className="control-hint">
              Resolve blockers by editing the task, adjusting the plan, or
              answering the flagged decision, then start the next cycle.
            </p>
          </div>
        ) : (
          <p className="control-hint">No open blockers.</p>
        )}
      </Panel>

      <Panel
        title="Recent cycle events"
        actions={
          <button
            className="btn"
            style={{ minHeight: 26 }}
            onClick={() => setExpanded(!expanded)}
            aria-expanded={expanded}
          >
            {expanded ? "collapse" : "expand"}
          </button>
        }
      >
        <KV
          items={[
            [
              "Last handoff",
              lastHandoff
                ? `${String(lastHandoff.from ?? lastHandoff.backend ?? "?")} → ${String(
                    lastHandoff.to ?? "?",
                  )} at ${formatTime(String(lastHandoff.ts))}`
                : "none recorded",
            ],
            [
              "Last repair trigger",
              lastRepair
                ? `${String(lastRepair.event)} at ${formatTime(String(lastRepair.ts))}`
                : "none recorded",
            ],
          ]}
        />
        {expanded && (
          <div className="log-view" style={{ marginTop: "var(--space-3)" }}>
            {(activity?.entries ?? [])
              .slice(-40)
              .map(
                (entry) =>
                  `${entry.ts ?? ""}  ${entry.event ?? ""}  ${JSON.stringify(
                    Object.fromEntries(
                      Object.entries(entry).filter(
                        ([key]) => !["ts", "event", "source"].includes(key),
                      ),
                    ),
                  )}\n`,
              )
              .join("") || "no events yet"}
          </div>
        )}
      </Panel>
    </div>
  );
}
