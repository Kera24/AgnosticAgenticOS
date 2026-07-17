/* Knowledge vault: generated Markdown docs, conflicts, validation, and a
   read-only viewer. Opening Obsidian stays a user action outside the UI. */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Drawer, KV, Panel, Skeleton, StatusChip } from "../components/ui";

type DocRow = {
  path: string;
  id: string | null;
  type: string | null;
  updated: string | null;
  conflict: boolean;
};

type KnowledgeView = {
  root: string;
  documents: number;
  conflicts: string[];
  issues: string[];
  docs: DocRow[];
};

type DocView = {
  path: string;
  meta: Record<string, unknown>;
  generated: string;
  user_section: string;
  generated_intact: boolean;
};

export function Knowledge() {
  const { data, isLoading } = useQuery({
    queryKey: ["knowledge"],
    queryFn: () => api.get<KnowledgeView>("/knowledge"),
  });
  const [selected, setSelected] = useState<string | null>(null);
  const doc = useQuery({
    queryKey: ["knowledge-doc", selected],
    queryFn: () =>
      api.get<DocView>(`/knowledge/doc?path=${encodeURIComponent(selected!)}`),
    enabled: selected !== null,
  });

  if (isLoading || !data)
    return (
      <div className="stack">
        <h1 className="visually-hidden">Knowledge vault</h1>
        <Panel title="Loading">
          <Skeleton />
        </Panel>
      </div>
    );

  const groups = new Map<string, DocRow[]>();
  for (const row of data.docs) {
    const dir = row.path.includes("/") ? row.path.split("/")[0] : "(root)";
    groups.set(dir, [...(groups.get(dir) ?? []), row]);
  }

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Knowledge Vault</h1>
        <span className="page-sub">
          Obsidian-compatible Markdown, generated deterministically — open{" "}
          <code className="mono">{data.root}</code> as a vault
        </span>
      </div>

      <div className="grid cols-2">
        <Panel title="Status">
          <KV
            items={[
              ["documents", data.documents],
              ["conflicts", data.conflicts.length],
              ["validation issues", data.issues.length],
            ]}
          />
          {data.conflicts.length > 0 && (
            <>
              <span className="eyebrow">pending conflicts</span>
              <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
                {data.conflicts.map((c) => (
                  <li key={c} className="mono">
                    {c} — your edit was preserved; review the .incoming.md
                  </li>
                ))}
              </ul>
            </>
          )}
        </Panel>
        <Panel title="Validation">
          {data.issues.length === 0 ? (
            <p className="control-hint">
              All documents valid: frontmatter present, links resolve,
              generated sections intact.
            </p>
          ) : (
            <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
              {data.issues.map((issue, i) => (
                <li key={i} className="mono" style={{ fontSize: 12 }}>
                  {issue}
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>

      {[...groups.entries()].map(([dir, rows]) => (
        <Panel key={dir} title={dir} flush>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Document</th>
                  <th scope="col">Type</th>
                  <th scope="col">Updated</th>
                  <th scope="col">State</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.path}
                    onClick={() => setSelected(row.path)}
                    style={{ cursor: "pointer" }}
                  >
                    <td className="mono strong">{row.path}</td>
                    <td>{row.type ?? "—"}</td>
                    <td>{row.updated ?? "—"}</td>
                    <td>
                      {row.conflict ? (
                        <StatusChip
                          outline
                          status={{ tone: "warn", label: "conflict" }}
                        />
                      ) : (
                        <StatusChip outline status={{ tone: "ok", label: "ok" }} />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      ))}

      <Drawer
        open={selected !== null}
        title={selected ?? ""}
        onClose={() => setSelected(null)}
      >
        {doc.data ? (
          <div className="stack">
            {!doc.data.generated_intact && (
              <StatusChip
                status={{
                  tone: "warn",
                  label: "generated section edited by user",
                }}
              />
            )}
            <pre
              className="code-block"
              style={{ whiteSpace: "pre-wrap", maxHeight: "60vh", overflow: "auto" }}
            >
              {doc.data.generated}
            </pre>
            {doc.data.user_section && (
              <>
                <span className="eyebrow">user notes</span>
                <pre className="code-block" style={{ whiteSpace: "pre-wrap" }}>
                  {doc.data.user_section}
                </pre>
              </>
            )}
          </div>
        ) : (
          <Skeleton />
        )}
      </Drawer>
    </div>
  );
}
