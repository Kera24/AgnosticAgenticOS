import { useMemo } from "react";
import { Link } from "react-router-dom";
import { FolderGit2, Waypoints } from "lucide-react";
import {
  useActivity,
  useBackends,
  useCapacity,
  useProject,
} from "../state/queries";
import { useLive } from "../state/events";
import {
  breakerStatus,
  decisionStatus,
  milestoneStatus,
  schedulerStatus,
  taskStatus,
} from "../lib/status";
import { formatTime, formatTokens } from "../lib/format";
import {
  deriveStages,
  OrchestrationRail,
} from "../components/OrchestrationRail";
import {
  BackendChip,
  Countdown,
  EmptyState,
  KV,
  Meter,
  Panel,
  Skeleton,
  StatusChip,
} from "../components/ui";

export function Overview() {
  const { data: project, isLoading } = useProject();
  const { data: activityData } = useActivity(400);
  const { data: capacity } = useCapacity();
  const { data: backendsData } = useBackends();
  const { liveActivity } = useLive();

  const entries = useMemo(() => {
    const base = activityData?.entries ?? [];
    const seen = new Set(base.map((e) => `${e.ts}|${e.event}`));
    const extra = liveActivity.filter(
      (e) => !seen.has(`${e.ts}|${e.event}`),
    );
    return [...base, ...extra];
  }, [activityData?.entries, liveActivity]);

  const stages = useMemo(
    () => deriveStages(entries, project),
    [entries, project],
  );

  if (isLoading) {
    return (
      <div className="stack">
        <h1 className="visually-hidden">Overview</h1>
        <Panel title="Loading">
          <Skeleton lines={4} />
        </Panel>
      </div>
    );
  }

  const scheduler = project?.scheduler;
  const progress = project?.progress ?? {};
  const byStatus = progress.tasks_by_status ?? {};
  const total = progress.tasks_total ?? 0;
  const done = byStatus.done ?? 0;
  const milestones = project?.milestones ?? [];
  const milestoneStates = progress.milestones ?? {};
  const milestonesDone = Object.values(milestoneStates).filter(
    (state) => state === "done",
  ).length;
  const requirements =
    project?.final_audit?.completion_criteria?.length ?? null;
  const lastCycle = [...entries]
    .reverse()
    .find((entry) => entry.event === "cycle_finished");
  const decision = capacity?.decision;
  const backends = backendsData?.backends ?? [];
  const usableBackends = backends.filter((b) => b.usable);

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Overview</h1>
        <span className="page-sub">
          {project?.exists
            ? "live orchestration state"
            : "no project started"}
        </span>
      </div>

      <Panel
        title="Orchestration"
        sub={
          scheduler?.state === "running"
            ? `cycle ${scheduler.current_cycle ?? ""} in flight`
            : "latest cycle"
        }
        flush
        actions={
          scheduler?.state === "cooling" ? (
            <Countdown
              until={scheduler.next_run_at}
              label="Cooling countdown"
            />
          ) : undefined
        }
      >
        <OrchestrationRail
          stages={stages}
          running={scheduler?.state === "running"}
        />
      </Panel>

      {!project?.exists ? (
        <Panel>
          <EmptyState
            icon={<Waypoints size={28} aria-hidden />}
            title="No project started"
            action={
              <Link to="/projects" className="btn primary big">
                <FolderGit2 size={16} aria-hidden /> Create a project
              </Link>
            }
          >
            Paste an application plan in Projects; the Architect turns it
            into milestones and a backlog, then cycles build it task by task.
          </EmptyState>
        </Panel>
      ) : (
        <div className="grid cols-2">
          <Panel title="Now">
            <KV
              items={[
                [
                  "State",
                  <StatusChip
                    key="s"
                    status={schedulerStatus(
                      scheduler?.state,
                      scheduler?.project_status,
                    )}
                    pulse={scheduler?.state === "running"}
                  />,
                ],
                ["Cycle", scheduler?.current_cycle ?? "—"],
                [
                  "Backend",
                  <BackendChip key="b" name={scheduler?.selected_backend} />,
                ],
                [
                  "Next task",
                  project?.next_task ? (
                    <>
                      <span className="mono">{project.next_task.id}</span>{" "}
                      — {project.next_task.description.slice(0, 80)}
                    </>
                  ) : (
                    "—"
                  ),
                ],
                [
                  "Next eligible run",
                  scheduler?.next_run_at
                    ? formatTime(scheduler.next_run_at)
                    : project?.eligible
                      ? "now"
                      : (project?.eligible_reason ?? "—"),
                ],
                [
                  "Last cycle",
                  lastCycle
                    ? `${String(lastCycle.outcome)} · ${formatTime(
                        String(lastCycle.ts),
                      )}`
                    : "none yet",
                ],
                [
                  "Capacity decision",
                  decision ? (
                    <StatusChip
                      key="d"
                      status={decisionStatus(decision.decision)}
                      title={decision.reason}
                    />
                  ) : (
                    "—"
                  ),
                ],
                [
                  "Est. next cycle",
                  capacity?.estimate
                    ? `${formatTokens(
                        capacity.estimate.required_capacity_tokens,
                      )} tokens (estimate)`
                    : "—",
                ],
              ]}
            />
          </Panel>

          <Panel title="Completion">
            <div className="stack" style={{ gap: "var(--space-3)" }}>
              {requirements !== null && (
                <Meter
                  label="Requirements"
                  done={project?.final_audit?.complete ? requirements : 0}
                  total={requirements}
                />
              )}
              <Meter
                label="Milestones"
                done={milestonesDone}
                total={milestones.length}
              />
              <Meter label="Tasks" done={done} total={total} tone="ok" />
              <div
                style={{
                  display: "flex",
                  gap: "var(--space-2)",
                  flexWrap: "wrap",
                }}
              >
                {Object.entries(byStatus).map(([status, count]) => (
                  <StatusChip
                    key={status}
                    outline
                    status={{
                      ...taskStatus(status),
                      label: `${count} ${status.replace("_", " ")}`,
                    }}
                  />
                ))}
              </div>
              {project?.final_audit && (
                <p className="control-hint">
                  Final audit:{" "}
                  {project.final_audit.complete
                    ? `complete at ${formatTime(project.final_audit.completed_at)}`
                    : "not passed yet"}
                </p>
              )}
            </div>
          </Panel>

          <Panel
            title="Attention"
            sub={`${project?.blockers.length ?? 0} open blockers`}
          >
            {project && project.blockers.length > 0 ? (
              <div className="stack" style={{ gap: "var(--space-2)" }}>
                {project.blockers.slice(0, 4).map((blocker, i) => (
                  <div
                    key={i}
                    className={`blocker-card${blocker.human_only ? " human" : ""}`}
                  >
                    <span className="eyebrow">
                      {blocker.human_only
                        ? "human decision required"
                        : `task ${blocker.task ?? "—"}`}
                    </span>
                    <span className="blocker-reason">{blocker.reason}</span>
                  </div>
                ))}
                {project.blockers.length > 4 && (
                  <Link to="/build">
                    view all {project.blockers.length} blockers
                  </Link>
                )}
              </div>
            ) : (
              <p className="control-hint">
                Nothing needs you right now. Blockers and human decisions
                appear here.
              </p>
            )}
          </Panel>

          <Panel
            title="Backends"
            sub={`${usableBackends.length}/${backends.length} usable`}
          >
            <div className="stack" style={{ gap: "var(--space-2)" }}>
              {backends.slice(0, 6).map((backend) => (
                <div
                  key={backend.name}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "var(--space-3)",
                  }}
                >
                  <BackendChip name={backend.name} />
                  <StatusChip
                    status={breakerStatus(backend.breaker_state)}
                    outline
                  />
                  {backend.is_primary && (
                    <span className="eyebrow">primary</span>
                  )}
                  <span style={{ flex: 1 }} />
                  <span className="control-hint">
                    {backend.detected
                      ? (backend.version ?? "detected")
                      : backend.classification === "api"
                        ? backend.auth
                        : "not detected"}
                  </span>
                </div>
              ))}
              <Link to="/backends" className="control-hint">
                backend detail →
              </Link>
            </div>
          </Panel>
        </div>
      )}

      {project?.exists && milestones.length > 0 && (
        <Panel title="Milestones" flush>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Milestone</th>
                  <th scope="col">Title</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {milestones.map((milestone) => (
                  <tr key={milestone.id}>
                    <td className="mono strong">{milestone.id}</td>
                    <td>{milestone.title ?? milestone.description ?? ""}</td>
                    <td>
                      <StatusChip
                        status={milestoneStatus(
                          milestoneStates[milestone.id] ?? "pending",
                        )}
                        outline
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
    </div>
  );
}
