/* Portfolio: the multi-project command centre — every registered
   application, exactly where it lives, what it's doing and why it waits. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../lib/api";
import {
  BackendChip,
  ConfirmDialog,
  Drawer,
  KV,
  Panel,
  Skeleton,
  StatusChip,
} from "../components/ui";
import type { Tone } from "../lib/status";

type Project = {
  id: string;
  name: string;
  root_path: string;
  plan_path: string | null;
  default_branch: string | null;
  agentic_branch: string;
  status: string;
  enabled: boolean;
  priority: number;
  state: string;
  waiting_reason: string;
  scheduler: {
    state: string;
    next_run_at: string | null;
    selected_backend: string | null;
    current_cycle: string | null;
  };
  progress: Record<string, number> | null;
  worktree: string;
  task_worktrees: string[];
  lease: { machine_id: string; pid: number; expires_at: string } | null;
  docker: { detected: boolean; compose_project: string | null };
  supabase: { detected: boolean; project_ref: string | null };
  code_index: { provider?: string; files_indexed?: number } | null;
  runtime_dir: string;
};

type PortfolioView = {
  projects: Project[];
  runtime_home: string;
  authorised_roots: string[];
};

type FleetView = {
  global_pause: boolean;
  limits: Record<string, unknown> & { maximum_active_projects: number };
  slots: {
    active_projects: number;
    model: number;
    per_backend: Record<string, number>;
    docker_build?: number;
    test_job?: number;
  };
  would_start: { project: string; backend: string }[];
  waiting: { project: string; reason: string }[];
};

const STATE_TONE: Record<string, Tone> = {
  running: "run", testing: "run", reviewing: "run", repairing: "warn",
  preparing: "run", queued: "idle", ready: "idle", cooling: "cool",
  paused: "idle", blocked: "block", failed: "fail", completed: "ok",
  uninitialised: "warn", archived: "idle",
};

export function Portfolio() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["portfolio"],
    queryFn: () => api.get<PortfolioView>("/portfolio"),
  });
  const { data: fleetData } = useQuery({
    queryKey: ["fleet"],
    queryFn: () => api.get<FleetView>("/fleet"),
  });
  const [selected, setSelected] = useState<Project | null>(null);
  const [confirm, setConfirm] = useState<{
    project: Project; action: "archive" | "remove" | "stop";
  } | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [form, setForm] = useState({ name: "", root: "", plan: "plan.md",
                                     create: false });
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["portfolio"] });
    queryClient.invalidateQueries({ queryKey: ["fleet"] });
  };

  const act = useMutation({
    mutationFn: ({ id, action, confirmed }:
      { id: string; action: string; confirmed?: boolean }) =>
      api.post(`/portfolio/${id}/${action}`,
               confirmed ? { confirm: true } : {}),
    onSuccess: () => { setConfirm(null); setError(null); refresh(); },
    onError: (err) => {
      setConfirm(null);
      setError(err instanceof ApiError ? err.message : String(err));
    },
  });

  const add = useMutation({
    mutationFn: () => api.post("/portfolio/add", form),
    onSuccess: () => { setAddOpen(false); setError(null); refresh(); },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : String(err)),
  });

  const fleetPause = useMutation({
    mutationFn: (pause: boolean) =>
      pause ? api.post("/fleet/pause", { confirm: true })
            : api.post("/fleet/resume"),
    onSuccess: refresh,
  });

  if (isLoading || !data)
    return (
      <div className="stack">
        <h1 className="visually-hidden">Portfolio</h1>
        <Panel title="Loading"><Skeleton /></Panel>
      </div>
    );

  const waitingByProject = new Map(
    (fleetData?.waiting ?? []).map((w) => [w.project, w.reason]),
  );

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Portfolio</h1>
        <span className="page-sub">
          runtime state in <code className="mono">{data.runtime_home}</code>
          {" "}· application folders stay exactly where you put them
        </span>
      </div>

      {error && (
        <Panel title="Action failed">
          <p className="control-hint" role="alert">{error}</p>
        </Panel>
      )}

      <Panel
        title="Scheduler"
        sub={fleetData?.global_pause ? "GLOBAL PAUSE ACTIVE" : undefined}
        actions={
          <span style={{ display: "flex", gap: 8 }}>
            <button className="btn" onClick={() => setAddOpen(true)}>
              Add project…
            </button>
            <button
              className={`btn ${fleetData?.global_pause ? "primary" : "danger"}`}
              onClick={() => fleetPause.mutate(!fleetData?.global_pause)}
            >
              {fleetData?.global_pause ? "Resume all" : "Emergency pause"}
            </button>
          </span>
        }
      >
        {fleetData ? (
          <KV
            items={[
              [
                "active projects",
                `${fleetData.slots.active_projects}/${fleetData.limits.maximum_active_projects}`,
              ],
              ["model slots in use", fleetData.slots.model ?? 0],
              [
                "per backend",
                Object.entries(fleetData.slots.per_backend ?? {})
                  .map(([b, n]) => `${b}:${n}`)
                  .join(" ") || "none",
              ],
              [
                "would start next tick",
                fleetData.would_start
                  .map((s) => `${s.project} (${s.backend})`)
                  .join(", ") || "nothing eligible",
              ],
            ]}
          />
        ) : (
          <Skeleton lines={2} />
        )}
      </Panel>

      <Panel title="Projects" flush>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Project</th>
                <th scope="col">State</th>
                <th scope="col">Why waiting</th>
                <th scope="col">Backend</th>
                <th scope="col" className="num">Prio</th>
                <th scope="col">Root</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.projects.length === 0 && (
                <tr>
                  <td colSpan={7} className="control-hint">
                    no projects registered — add an existing folder or
                    create a new one
                  </td>
                </tr>
              )}
              {data.projects.map((p) => (
                <tr key={p.id}>
                  <td>
                    <button className="btn" style={{ minHeight: 24 }}
                            onClick={() => setSelected(p)}>
                      <span className="mono strong">{p.id}</span>
                    </button>
                  </td>
                  <td>
                    <StatusChip
                      outline
                      status={{
                        tone: STATE_TONE[p.state] ?? "idle",
                        label: p.state,
                      }}
                    />
                  </td>
                  <td style={{ maxWidth: 260 }}>
                    {waitingByProject.get(p.id) ?? p.waiting_reason}
                  </td>
                  <td>
                    <BackendChip name={p.scheduler.selected_backend} />
                  </td>
                  <td className="num">{p.priority}</td>
                  <td className="mono" style={{ fontSize: 12 }}>
                    {p.root_path}
                  </td>
                  <td>
                    <span style={{ display: "flex", gap: 4 }}>
                      {p.status === "registered" && (
                        <button className="btn" style={{ minHeight: 24 }}
                          onClick={() =>
                            act.mutate({ id: p.id, action: "init" })}>
                          init
                        </button>
                      )}
                      {p.state === "paused" ? (
                        <button className="btn" style={{ minHeight: 24 }}
                          onClick={() =>
                            act.mutate({ id: p.id, action: "resume" })}>
                          resume
                        </button>
                      ) : (
                        <button className="btn" style={{ minHeight: 24 }}
                          onClick={() =>
                            act.mutate({ id: p.id, action: "pause" })}>
                          pause
                        </button>
                      )}
                      <button className="btn" style={{ minHeight: 24 }}
                        onClick={() =>
                          setConfirm({ project: p, action: "stop" })}>
                        stop…
                      </button>
                      <button className="btn" style={{ minHeight: 24 }}
                        onClick={() =>
                          setConfirm({ project: p, action: "archive" })}>
                        archive…
                      </button>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Drawer open={selected !== null} title={selected?.name ?? ""}
              onClose={() => setSelected(null)}>
        {selected && (
          <div className="stack">
            <KV
              items={[
                ["id", selected.id],
                ["root", selected.root_path],
                ["plan", selected.plan_path ?? "—"],
                ["branches",
                 `${selected.default_branch ?? "?"} → ${selected.agentic_branch}`],
                ["integration worktree", selected.worktree],
                ["task worktrees",
                 selected.task_worktrees.join(", ") || "none"],
                ["lease",
                 selected.lease
                   ? `${selected.lease.machine_id} pid ${selected.lease.pid} until ${selected.lease.expires_at}`
                   : "free"],
                ["docker",
                 selected.docker.detected
                   ? `detected · ${selected.docker.compose_project}`
                   : "not detected"],
                ["supabase",
                 selected.supabase.detected
                   ? `detected${selected.supabase.project_ref ? ` · ref ${selected.supabase.project_ref}` : ""}`
                   : "not detected"],
                ["code index",
                 selected.code_index
                   ? `${selected.code_index.provider} · ${selected.code_index.files_indexed} files`
                   : "not built"],
                ["progress",
                 selected.progress
                   ? Object.entries(selected.progress)
                       .map(([k, v]) => `${k}:${v}`)
                       .join(" ")
                   : "not started"],
                ["runtime state", selected.runtime_dir],
              ]}
            />
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button className="btn"
                onClick={() =>
                  act.mutate({ id: selected.id, action: "doctor" })}>
                Run doctor
              </button>
              <button className="btn danger"
                onClick={() =>
                  setConfirm({ project: selected, action: "remove" })}>
                Remove from registry…
              </button>
            </div>
          </div>
        )}
      </Drawer>

      <ConfirmDialog
        open={confirm !== null}
        title={`${confirm?.action} project`}
        body={
          <p>
            {confirm?.action === "remove" ? (
              <>Forget <code>{confirm?.project.id}</code> from the
              registry? The application folder and its files are NOT
              deleted.</>
            ) : confirm?.action === "archive" ? (
              <>Archive <code>{confirm?.project.id}</code>? It stops being
              scheduled; all files stay untouched.</>
            ) : (
              <>Stop <code>{confirm?.project.id}</code>? The scheduler
              will no longer pick it up until resumed.</>
            )}
          </p>
        }
        confirmLabel={confirm?.action ?? ""}
        danger
        working={act.isPending}
        onConfirm={() =>
          confirm &&
          act.mutate({ id: confirm.project.id, action: confirm.action,
                       confirmed: true })}
        onCancel={() => setConfirm(null)}
      />

      <ConfirmDialog
        open={addOpen}
        title="Add project"
        body={
          <div className="stack" style={{ gap: 8 }}>
            <label>
              Name
              <input className="input" value={form.name}
                onChange={(e) =>
                  setForm({ ...form, name: e.target.value })} />
            </label>
            <label>
              Folder (absolute path)
              <input className="input" value={form.root}
                placeholder="C:\Agentic\projects\my-app"
                onChange={(e) =>
                  setForm({ ...form, root: e.target.value })} />
            </label>
            <label>
              Plan file (relative)
              <input className="input" value={form.plan}
                onChange={(e) =>
                  setForm({ ...form, plan: e.target.value })} />
            </label>
            <label style={{ display: "flex", gap: 6 }}>
              <input type="checkbox" checked={form.create}
                onChange={(e) =>
                  setForm({ ...form, create: e.target.checked })} />
              create a new folder (with git + plan template)
            </label>
          </div>
        }
        confirmLabel="Register"
        working={add.isPending}
        onConfirm={() => add.mutate()}
        onCancel={() => setAddOpen(false)}
      />
    </div>
  );
}
