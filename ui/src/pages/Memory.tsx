/* Memory: progressive disclosure (search -> timeline -> details) over the
   OS-owned store, with confirmed forget. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { formatTime } from "../lib/format";
import {
  ConfirmDialog,
  Drawer,
  KV,
  Panel,
  Skeleton,
  StatusChip,
} from "../components/ui";

type MemoryRow = {
  id: string;
  type: string;
  title: string;
  compact_summary: string;
  importance: number;
  reviewer_verified: number;
  status: string;
  created_at: string;
  task_id: string | null;
};

type MemoryView = {
  project_id: string;
  total_records: number;
  by_type: Record<string, Record<string, number>>;
  records: MemoryRow[];
};

type MemoryDetail = MemoryRow & {
  details: string | null;
  source: string | null;
  supersedes: string | null;
  tags: string[];
  related_paths: string[];
  sensitive: number;
  updated_at: string;
};

export function Memory() {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [searched, setSearched] = useState("");
  const [includeSuperseded, setIncludeSuperseded] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [confirmForget, setConfirmForget] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["memory", searched, includeSuperseded],
    queryFn: () =>
      api.get<MemoryView>(
        `/memory?q=${encodeURIComponent(searched)}&include_superseded=${includeSuperseded}`,
      ),
  });

  const detail = useQuery({
    queryKey: ["memory-detail", selected],
    queryFn: async () => {
      const [records, timeline] = await Promise.all([
        api.get<{ records: MemoryDetail[] }>(`/memory/records?ids=${selected}`),
        api.get<{ timeline: MemoryRow[] }>(`/memory/${selected}/timeline`),
      ]);
      return { record: records.records[0], timeline: timeline.timeline };
    },
    enabled: selected !== null,
  });

  const forget = useMutation({
    mutationFn: (id: string) =>
      api.post("/memory/forget", { id, confirm: true }),
    onSuccess: () => {
      setConfirmForget(null);
      setSelected(null);
      queryClient.invalidateQueries({ queryKey: ["memory"] });
    },
  });

  if (isLoading || !data)
    return (
      <div className="stack">
        <h1 className="visually-hidden">Memory</h1>
        <Panel title="Loading">
          <Skeleton />
        </Panel>
      </div>
    );

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Memory</h1>
        <span className="page-sub">
          {data.total_records} records · project {data.project_id} · compact
          summaries load first, details on demand
        </span>
      </div>

      <Panel title="Search">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setSearched(query.trim());
          }}
          style={{ display: "flex", gap: 8, flexWrap: "wrap" }}
        >
          <input
            className="input"
            style={{ flex: 1, minWidth: 220 }}
            placeholder="search decisions, failures, findings…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Memory search query"
          />
          <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={includeSuperseded}
              onChange={(e) => setIncludeSuperseded(e.target.checked)}
            />
            include superseded
          </label>
          <button className="btn primary" type="submit">
            Search
          </button>
        </form>
      </Panel>

      <Panel title="Records" flush>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Type</th>
                <th scope="col">Title</th>
                <th scope="col">Summary</th>
                <th scope="col">Status</th>
                <th scope="col">Verified</th>
              </tr>
            </thead>
            <tbody>
              {data.records.length === 0 && (
                <tr>
                  <td colSpan={6} className="control-hint">
                    no records{searched ? ` matching “${searched}”` : ""}
                  </td>
                </tr>
              )}
              {data.records.map((r) => (
                <tr
                  key={r.id}
                  onClick={() => setSelected(r.id)}
                  style={{ cursor: "pointer" }}
                >
                  <td>{formatTime(r.created_at)}</td>
                  <td className="mono">{r.type}</td>
                  <td className="strong">{r.title}</td>
                  <td>{r.compact_summary}</td>
                  <td>
                    <StatusChip
                      outline
                      status={
                        r.status === "active"
                          ? { tone: "ok", label: "active" }
                          : r.status === "superseded"
                            ? { tone: "idle", label: "superseded" }
                            : { tone: "warn", label: r.status }
                      }
                    />
                  </td>
                  <td>{r.reviewer_verified ? "yes" : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Drawer
        open={selected !== null}
        title={`record ${selected ?? ""}`}
        onClose={() => setSelected(null)}
      >
        {detail.data ? (
          <div className="stack">
            <KV
              items={[
                ["type", detail.data.record?.type],
                ["title", detail.data.record?.title],
                ["summary", detail.data.record?.compact_summary],
                ["details", detail.data.record?.details ?? "(none stored)"],
                ["source", detail.data.record?.source ?? "—"],
                ["status", detail.data.record?.status],
                ["supersedes", detail.data.record?.supersedes ?? "—"],
                ["tags", (detail.data.record?.tags ?? []).join(", ") || "—"],
              ]}
            />
            <span className="eyebrow">timeline (surrounding events)</span>
            <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
              {detail.data.timeline.map((t) => (
                <li
                  key={t.id}
                  className="mono"
                  style={{
                    fontSize: 12,
                    fontWeight: t.id === selected ? 700 : 400,
                  }}
                >
                  {formatTime(t.created_at)} [{t.type}] {t.title}
                </li>
              ))}
            </ul>
            <button
              className="btn danger"
              onClick={() => setConfirmForget(selected)}
            >
              Forget this record…
            </button>
          </div>
        ) : (
          <Skeleton />
        )}
      </Drawer>

      <ConfirmDialog
        open={confirmForget !== null}
        title="Forget memory record"
        body={
          <p>
            Permanently delete record <code>{confirmForget}</code>? This
            cannot be undone.
          </p>
        }
        confirmLabel="Forget"
        danger
        working={forget.isPending}
        onConfirm={() => confirmForget && forget.mutate(confirmForget)}
        onCancel={() => setConfirmForget(null)}
      />
    </div>
  );
}
