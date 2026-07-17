/* Skills: pinned, checksummed, reviewed instruction packages. Enabling and
   disabling are confirmed admin actions; installation stays in the CLI. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../lib/api";
import { ConfirmDialog, Drawer, KV, Panel, Skeleton, StatusChip } from "../components/ui";

type Skill = {
  id: string;
  name: string;
  description: string;
  source: string;
  pinned_revision: string;
  checksum: string;
  license: string | null;
  compatible_agents: string[];
  triggers: string[];
  scripts: string[];
  permissions: string[];
  reviewed: boolean;
  reviewed_at: string | null;
  enabled: boolean;
  risk_level: string;
  integrity_failure?: string;
  scan_findings?: string[];
};

export function Skills() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["skills"],
    queryFn: () => api.get<{ skills: Skill[] }>("/skills"),
  });
  const [inspect, setInspect] = useState<Skill | null>(null);
  const [confirm, setConfirm] = useState<{
    skill: Skill;
    action: "enable" | "disable";
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const act = useMutation({
    mutationFn: ({ skill, action }: { skill: Skill; action: string }) =>
      api.post(`/skills/${skill.id}/${action}`, { confirm: true }),
    onSuccess: () => {
      setConfirm(null);
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
    onError: (err) => {
      setConfirm(null);
      setError(err instanceof ApiError ? err.message : String(err));
    },
  });

  if (isLoading || !data)
    return (
      <div className="stack">
        <h1 className="visually-hidden">Skills</h1>
        <Panel title="Loading">
          <Skeleton />
        </Panel>
      </div>
    );

  const riskTone = (risk: string) =>
    risk === "low" ? "ok" : risk === "medium" ? "warn" : "block";

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Skills</h1>
        <span className="page-sub">
          pinned + checksummed; loaded on demand only; instructions rank below
          OS policy. Install via CLI:{" "}
          <code className="mono">run skills add &lt;dir&gt; --revision &lt;commit&gt;</code>
        </span>
      </div>

      {error && (
        <Panel title="Action failed">
          <p className="control-hint" role="alert">
            {error}
          </p>
        </Panel>
      )}

      <Panel title="Installed skills" flush>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Skill</th>
                <th scope="col">Source</th>
                <th scope="col">Revision</th>
                <th scope="col">Agents</th>
                <th scope="col">Risk</th>
                <th scope="col">Reviewed</th>
                <th scope="col">State</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.skills.length === 0 && (
                <tr>
                  <td colSpan={8} className="control-hint">
                    no skills installed
                  </td>
                </tr>
              )}
              {data.skills.map((skill) => (
                <tr key={skill.id}>
                  <td>
                    <button
                      className="btn"
                      style={{ minHeight: 24 }}
                      onClick={() => setInspect(skill)}
                    >
                      <span className="mono strong">{skill.id}</span>
                    </button>
                  </td>
                  <td className="mono">{skill.source === "builtin" ? "builtin" : "installed"}</td>
                  <td className="mono">{skill.pinned_revision.slice(0, 10)}</td>
                  <td className="mono">
                    {(skill.compatible_agents ?? []).join(", ")}
                  </td>
                  <td>
                    <StatusChip
                      outline
                      status={{
                        tone: riskTone(skill.risk_level) as "ok",
                        label: skill.risk_level,
                      }}
                    />
                  </td>
                  <td>{skill.reviewed ? "yes" : "no"}</td>
                  <td>
                    {skill.integrity_failure ? (
                      <StatusChip
                        status={{ tone: "fail", label: "integrity failed" }}
                        title={skill.integrity_failure}
                      />
                    ) : (
                      <StatusChip
                        outline
                        status={
                          skill.enabled
                            ? { tone: "ok", label: "enabled" }
                            : { tone: "idle", label: "disabled" }
                        }
                      />
                    )}
                  </td>
                  <td>
                    <button
                      className="btn"
                      style={{ minHeight: 24 }}
                      onClick={() =>
                        setConfirm({
                          skill,
                          action: skill.enabled ? "disable" : "enable",
                        })
                      }
                    >
                      {skill.enabled ? "disable…" : "enable…"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Drawer
        open={inspect !== null}
        title={inspect?.name ?? ""}
        onClose={() => setInspect(null)}
      >
        {inspect && (
          <div className="stack">
            <p>{inspect.description}</p>
            <KV
              items={[
                ["id", inspect.id],
                ["source", inspect.source],
                ["pinned revision", inspect.pinned_revision],
                ["checksum", inspect.checksum.slice(0, 20) + "…"],
                ["license", inspect.license ?? "unknown"],
                ["triggers", (inspect.triggers ?? []).join(", ") || "—"],
                ["permissions", (inspect.permissions ?? []).join(", ")],
                ["scripts", (inspect.scripts ?? []).join(", ") || "none"],
                ["reviewed at", inspect.reviewed_at ?? "—"],
              ]}
            />
            {(inspect.scan_findings ?? []).length > 0 && (
              <>
                <span className="eyebrow">scan findings</span>
                <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
                  {inspect.scan_findings!.map((f, i) => (
                    <li key={i} className="mono" style={{ fontSize: 12 }}>
                      {f}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )}
      </Drawer>

      <ConfirmDialog
        open={confirm !== null}
        title={confirm?.action === "enable" ? "Enable skill" : "Disable skill"}
        body={
          <p>
            {confirm?.action === "enable" ? (
              <>
                Enable <code>{confirm?.skill.id}</code>? Its instructions
                become eligible for injection into matching agent prompts
                (as untrusted content, within the skills budget).
              </>
            ) : (
              <>
                Disable <code>{confirm?.skill.id}</code>? It will never be
                selected or loaded until re-enabled.
              </>
            )}
          </p>
        }
        confirmLabel={confirm?.action === "enable" ? "Enable" : "Disable"}
        danger={confirm?.action === "disable"}
        working={act.isPending}
        onConfirm={() => confirm && act.mutate(confirm)}
        onCancel={() => setConfirm(null)}
      />
    </div>
  );
}
