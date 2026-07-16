import { useState } from "react";
import { ClipboardCopy, FileText, FolderGit2 } from "lucide-react";
import {
  useBacklog,
  usePlan,
  usePlanPreview,
  useProject,
  useStartProject,
} from "../state/queries";
import { useLive } from "../state/events";
import { milestoneStatus, taskStatus } from "../lib/status";
import { formatTime } from "../lib/format";
import {
  EmptyState,
  KV,
  Panel,
  Skeleton,
  StatusChip,
  Working,
} from "../components/ui";

function CopyButton({ value, label }: { value: string; label: string }) {
  const { pushToast } = useLive();
  return (
    <button
      className="btn"
      style={{ minHeight: 26 }}
      onClick={() =>
        navigator.clipboard
          .writeText(value)
          .then(() => pushToast({ tone: "ok", title: "Copied", detail: label }))
      }
      aria-label={`Copy ${label}`}
    >
      <ClipboardCopy size={13} aria-hidden />
    </button>
  );
}

function NewProjectForm() {
  const [mode, setMode] = useState<"paste" | "file">("paste");
  const [planText, setPlanText] = useState("");
  const [planPath, setPlanPath] = useState("");
  const preview = usePlanPreview();
  const start = useStartProject();
  const { pushToast } = useLive();

  const body =
    mode === "paste" ? { plan_text: planText } : { plan_path: planPath };
  const filled = mode === "paste" ? planText.trim().length >= 40 : planPath.trim().length > 0;

  return (
    <div className="stack">
      <div style={{ display: "flex", gap: "var(--space-2)" }} role="group" aria-label="Plan source">
        <button
          className={`btn${mode === "paste" ? " primary" : ""}`}
          onClick={() => setMode("paste")}
          aria-pressed={mode === "paste"}
        >
          Paste plan
        </button>
        <button
          className={`btn${mode === "file" ? " primary" : ""}`}
          onClick={() => setMode("file")}
          aria-pressed={mode === "file"}
        >
          Use a file in the repository
        </button>
      </div>

      {mode === "paste" ? (
        <div className="field">
          <label htmlFor="plan-text">Application plan (Markdown)</label>
          <textarea
            id="plan-text"
            className="input"
            value={planText}
            onChange={(e) => setPlanText(e.target.value)}
            placeholder={"# My application\n\nDescribe what to build, its requirements and constraints…"}
          />
          <span className="field-help">
            The Project Architect reads the full plan and produces
            architecture, milestones, backlog and acceptance criteria.
          </span>
          {planText.trim().length > 0 && planText.trim().length < 40 && (
            <span className="field-error">
              The plan is too short — describe the application in at least a
              few sentences.
            </span>
          )}
        </div>
      ) : (
        <div className="field">
          <label htmlFor="plan-path">
            Markdown file path (inside this repository)
          </label>
          <input
            id="plan-path"
            className="input"
            value={planPath}
            onChange={(e) => setPlanPath(e.target.value)}
            placeholder="plans\\my-app.md"
          />
          <span className="field-help">
            The path is validated server-side: it must resolve inside the
            repository root and be a .md/.txt file.
          </span>
        </div>
      )}

      <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
        <button
          className="btn"
          disabled={!filled || preview.isPending}
          title={!filled ? "provide a plan first" : undefined}
          onClick={() => preview.mutate(body)}
        >
          {preview.isPending ? <Working label="Preview" /> : "Preview plan"}
        </button>
        <button
          className="btn primary"
          disabled={!filled || start.isPending}
          title={!filled ? "provide a plan first" : undefined}
          onClick={() =>
            start.mutate(body, {
              onSuccess: () =>
                pushToast({
                  tone: "run",
                  title: "Architecting project",
                  detail:
                    "the Architect is designing milestones and a backlog — this can take a few minutes",
                }),
              onError: (error) =>
                pushToast({
                  tone: "fail",
                  title: "Could not start project",
                  detail: error.message,
                  sticky: true,
                }),
            })
          }
        >
          {start.isPending ? <Working label="Starting…" /> : "Start project"}
        </button>
      </div>

      {preview.isError && (
        <p className="field-error">{preview.error.message}</p>
      )}
      {preview.data && (
        <div className="stack" style={{ gap: "var(--space-2)" }}>
          <span className="eyebrow">
            preview · {preview.data.source} · {preview.data.length} chars
          </span>
          <div className="log-view">{preview.data.content}</div>
        </div>
      )}
    </div>
  );
}

export function Projects() {
  const { data: project, isLoading } = useProject();
  const { data: planDocs } = usePlan(Boolean(project?.exists));
  const { data: backlogData } = useBacklog();
  const [tab, setTab] = useState<
    "architecture" | "backlog" | "criteria" | "decisions" | "audit"
  >("architecture");

  if (isLoading)
    return (
      <div className="stack">
        <h1 className="visually-hidden">Projects</h1>
        <Panel title="Loading">
          <Skeleton />
        </Panel>
      </div>
    );

  if (!project?.exists) {
    return (
      <div className="stack">
        <div className="page-head">
          <h1>Projects</h1>
          <span className="page-sub">one project at a time, built to audit</span>
        </div>
        <Panel title="New project">
          <NewProjectForm />
        </Panel>
        <Panel title="How it works">
          <EmptyState
            icon={<FolderGit2 size={26} aria-hidden />}
            title="The OS repository stays untouched"
          >
            Generated work lives on the{" "}
            <span className="mono">agentic/project</span> branch in an
            isolated worktree. Merging to main is always your act — the
            dashboard has no push, merge or deploy capability.
          </EmptyState>
        </Panel>
      </div>
    );
  }

  const tasks = backlogData?.tasks ?? [];
  const criteria = planDocs?.acceptance_criteria;
  const audit = project.final_audit;

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Project: {project.name}</h1>
        <span className="page-sub">
          {project.progress.tasks_total ?? 0} tasks ·{" "}
          {project.milestones.length} milestones
        </span>
      </div>

      <Panel title="Location">
        <KV
          items={[
            [
              "OS repository",
              <span key="r" style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
                <span className="mono">{project.repository_root}</span>
                <CopyButton value={project.repository_root} label="repository path" />
              </span>,
            ],
            [
              "Project branch",
              <span key="b" style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
                <span className="mono">{project.branch}</span>
                <CopyButton value={project.branch} label="branch name" />
              </span>,
            ],
            [
              "Worktree",
              <span key="w" style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
                <span className="mono">{project.worktree}</span>
                <CopyButton value={project.worktree} label="worktree path" />
                {!project.worktree_exists && (
                  <span className="control-hint">(created on first cycle)</span>
                )}
              </span>,
            ],
            [
              "Review command",
              <span key="c" style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
                <span className="mono">git log agentic/project --oneline</span>
                <CopyButton
                  value="git log agentic/project --oneline"
                  label="review command"
                />
              </span>,
            ],
          ]}
        />
        <p className="control-hint" style={{ marginTop: "var(--space-3)" }}>
          The generated application accumulates on the project branch; this
          repository's own files are never modified by cycles.
        </p>
      </Panel>

      <Panel
        flush
        title="Project detail"
        actions={
          <span role="tablist" aria-label="Project detail sections" style={{ display: "flex", gap: 4 }}>
            {(
              [
                ["architecture", "Architecture"],
                ["backlog", "Backlog"],
                ["criteria", "Acceptance"],
                ["decisions", "Decisions"],
                ["audit", "Final audit"],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                role="tab"
                aria-selected={tab === id}
                className={`btn${tab === id ? " primary" : ""}`}
                style={{ minHeight: 26 }}
                onClick={() => setTab(id)}
              >
                {label}
              </button>
            ))}
          </span>
        }
      >
        {tab === "architecture" && (
          <div className="panel-body">
            {planDocs?.architecture ? (
              <div className="log-view" style={{ maxHeight: 480 }}>
                {planDocs.architecture}
              </div>
            ) : (
              <Skeleton />
            )}
          </div>
        )}

        {tab === "backlog" && (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Task</th>
                  <th scope="col">Milestone</th>
                  <th scope="col">Description</th>
                  <th scope="col">Status</th>
                  <th scope="col" className="num">
                    Attempts
                  </th>
                  <th scope="col">Blocking reason</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => (
                  <tr
                    key={task.id}
                    className={
                      task.status === "blocked" ? "attention" : undefined
                    }
                  >
                    <td className="mono strong">{task.id}</td>
                    <td className="mono">{task.milestone ?? "—"}</td>
                    <td>{task.description}</td>
                    <td>
                      <StatusChip status={taskStatus(task.status)} outline />
                    </td>
                    <td className="num">{task.attempts}</td>
                    <td>{task.blocking_reason ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {tab === "criteria" && (
          <div className="panel-body stack">
            <div>
              <h3 className="eyebrow" style={{ marginBottom: "var(--space-2)" }}>
                completion criteria
              </h3>
              <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
                {(criteria?.completion_criteria ?? []).map((item, i) => (
                  <li key={i}>{item}</li>
                ))}
              </ul>
            </div>
            {criteria?.requirements_map &&
              criteria.requirements_map.length > 0 && (
                <div className="table-scroll">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th scope="col">Requirement</th>
                        <th scope="col">Tasks</th>
                      </tr>
                    </thead>
                    <tbody>
                      {criteria.requirements_map.map((row, i) => (
                        <tr key={i}>
                          <td>{row.requirement}</td>
                          <td className="mono">
                            {(row.tasks ?? []).join(", ")}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
          </div>
        )}

        {tab === "decisions" && (
          <div className="panel-body">
            {project.human_decisions.length === 0 ? (
              <p className="control-hint">
                The Architect flagged no decisions that need you.
              </p>
            ) : (
              <div className="stack" style={{ gap: "var(--space-2)" }}>
                {project.human_decisions.map((decision, i) => (
                  <div key={i} className="blocker-card human">
                    <span className="eyebrow">decision needed</span>
                    <span className="blocker-reason">{decision}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {tab === "audit" && (
          <div className="panel-body">
            {!audit ? (
              <EmptyState
                icon={<FileText size={24} aria-hidden />}
                title="No final audit yet"
              >
                The audit runs automatically when the backlog completes, or
                on demand from Build Control.
              </EmptyState>
            ) : (
              <div className="stack" style={{ gap: "var(--space-3)" }}>
                <StatusChip
                  status={
                    audit.complete
                      ? { tone: "ok", label: "Complete" }
                      : { tone: "fail", label: "Audit failed" }
                  }
                />
                <KV
                  items={[
                    ["Audited at", formatTime(audit.completed_at)],
                    ["Branch", <span key="b" className="mono">{audit.branch}</span>],
                  ]}
                />
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">Check</th>
                      <th scope="col">Result</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(audit.checks).map(([check, passed]) => (
                      <tr key={check} className={passed ? undefined : "failure"}>
                        <td className="mono">{check.replaceAll("_", " ")}</td>
                        <td>
                          <StatusChip
                            outline
                            status={
                              passed
                                ? { tone: "ok", label: "Pass" }
                                : { tone: "fail", label: "Fail" }
                            }
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </Panel>

      {(project.milestones.length > 0) && (
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
                {project.milestones.map((milestone) => (
                  <tr key={milestone.id}>
                    <td className="mono strong">{milestone.id}</td>
                    <td>{milestone.title ?? milestone.description ?? ""}</td>
                    <td>
                      <StatusChip
                        outline
                        status={milestoneStatus(
                          project.progress.milestones?.[milestone.id] ??
                            "pending",
                        )}
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
