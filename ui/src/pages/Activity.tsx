/* Activity: the chronological audit trail (decisions.jsonl, redacted at the
   source). Filterable by cycle/agent/backend/severity, searchable, with
   expandable evidence and copy-as-JSON. Malformed lines are shown honestly. */
import { useMemo, useState } from "react";
import { ClipboardCopy, ListFilter } from "lucide-react";
import { useActivity } from "../state/queries";
import { useLive } from "../state/events";
import { activityTone } from "../lib/status";
import { formatTime } from "../lib/format";
import { EmptyState, Panel, Skeleton } from "../components/ui";
import type { ActivityEntry } from "../lib/types";

const SEVERITY: Record<string, string[]> = {
  failures: [
    "backend_error",
    "gate_failed",
    "scope_violation",
    "malformed",
    "malformed_output",
    "alert",
  ],
  routing: ["fallback", "handoff", "routing_skip", "backend_build_failed"],
  reviews: ["qa_review", "security_review"],
  lifecycle: [
    "project_started",
    "capacity_decision",
    "cycle_finished",
    "ui_project_start",
    "ui_project_run",
    "ui_project_pause",
    "ui_project_resume",
    "ui_final_audit",
  ],
};

function entryTitle(entry: ActivityEntry): string {
  const event = String(entry.event ?? "event");
  switch (event) {
    case "capacity_decision":
      return `capacity: ${entry.decision} (${entry.confidence}) → ${entry.selected_backend ?? "—"}`;
    case "cycle_finished":
      return `cycle ${entry.run_id}: ${entry.outcome}`;
    case "qa_review":
      return `QA verdict: ${entry.verdict}`;
    case "security_review":
      return `security verdict: ${entry.verdict}`;
    case "fallback":
      return `fallback ${entry.from} → ${entry.to} (${entry.reason})`;
    case "handoff":
      return `handoff ${entry.from} → ${entry.to}`;
    case "backend_error":
      return `${entry.backend}: ${entry.kind}`;
    case "gate_failed":
      return `gate failed (attempt ${entry.attempt})`;
    case "project_started":
      return `project started: ${entry.tasks} tasks, ${entry.milestones} milestones`;
    case "malformed":
      return "malformed log line";
    default:
      return event.replaceAll("_", " ");
  }
}

export function Activity() {
  const { data, isLoading } = useActivity(1000);
  const { liveActivity } = useLive();
  const [search, setSearch] = useState("");
  const [cycle, setCycle] = useState("all");
  const [backend, setBackend] = useState("all");
  const [severity, setSeverity] = useState("all");
  const [expandedIndex, setExpandedIndex] = useState<number | null>(null);

  const entries = useMemo(() => {
    const base = data?.entries ?? [];
    const seen = new Set(base.map((e) => JSON.stringify(e)));
    return [
      ...base,
      ...liveActivity.filter((e) => !seen.has(JSON.stringify(e))),
    ];
  }, [data?.entries, liveActivity]);

  const cycles = useMemo(
    () =>
      [...new Set(entries.map((e) => String(e.run_id ?? "")).filter(Boolean))]
        .slice(-30)
        .reverse(),
    [entries],
  );
  const backends = useMemo(
    () =>
      [
        ...new Set(
          entries
            .map((e) => String(e.backend ?? e.to ?? ""))
            .filter(Boolean),
        ),
      ].sort(),
    [entries],
  );

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return entries
      .filter((entry) => {
        if (cycle !== "all" && String(entry.run_id ?? "") !== cycle)
          return false;
        if (
          backend !== "all" &&
          String(entry.backend ?? "") !== backend &&
          String(entry.to ?? "") !== backend
        )
          return false;
        if (
          severity !== "all" &&
          !SEVERITY[severity]?.includes(String(entry.event ?? ""))
        )
          return false;
        if (q && !JSON.stringify(entry).toLowerCase().includes(q))
          return false;
        return true;
      })
      .slice(-400)
      .reverse();
  }, [entries, search, cycle, backend, severity]);

  if (isLoading)
    return (
      <div className="stack">
        <h1 className="visually-hidden">Activity</h1>
        <Panel title="Loading">
          <Skeleton />
        </Panel>
      </div>
    );

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Activity</h1>
        <span className="page-sub">
          append-only audit trail · redacted before display
        </span>
      </div>

      <Panel flush>
        <div
          style={{
            display: "flex",
            gap: "var(--space-3)",
            padding: "var(--space-3) var(--space-4)",
            borderBottom: "1px solid var(--border-hairline)",
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <ListFilter size={14} aria-hidden />
          <input
            className="input"
            style={{ maxWidth: 240 }}
            placeholder="Search events…"
            aria-label="Search events"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <select
            className="input"
            style={{ maxWidth: 170 }}
            aria-label="Filter by cycle"
            value={cycle}
            onChange={(e) => setCycle(e.target.value)}
          >
            <option value="all">all cycles</option>
            {cycles.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
          <select
            className="input"
            style={{ maxWidth: 150 }}
            aria-label="Filter by backend"
            value={backend}
            onChange={(e) => setBackend(e.target.value)}
          >
            <option value="all">all backends</option>
            {backends.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <select
            className="input"
            style={{ maxWidth: 150 }}
            aria-label="Filter by category"
            value={severity}
            onChange={(e) => setSeverity(e.target.value)}
          >
            <option value="all">all categories</option>
            <option value="failures">failures</option>
            <option value="routing">routing</option>
            <option value="reviews">reviews</option>
            <option value="lifecycle">lifecycle</option>
          </select>
          <span className="control-hint" style={{ marginLeft: "auto" }}>
            {filtered.length} events
          </span>
        </div>

        {filtered.length === 0 ? (
          <EmptyState title="No events match">
            {entries.length === 0
              ? "The trail fills as soon as a project starts or a cycle runs."
              : "Loosen the filters or clear the search."}
          </EmptyState>
        ) : (
          <div
            className="timeline"
            style={{ padding: "var(--space-3) var(--space-4)" }}
            aria-live="off"
          >
            {filtered.map((entry, index) => {
              const expanded = expandedIndex === index;
              const tone = activityTone(String(entry.event));
              return (
                <div className="timeline-entry" key={index}>
                  <span className="t-time">{formatTime(String(entry.ts))}</span>
                  <span className="t-marker" aria-hidden>
                    <span className={`t-dot tone-${tone}`} />
                  </span>
                  <div className="t-body">
                    <div className="t-title">
                      <button
                        className="btn"
                        style={{
                          minHeight: 0,
                          border: "none",
                          background: "none",
                          padding: 0,
                          color: "var(--text-primary)",
                        }}
                        aria-expanded={expanded}
                        onClick={() =>
                          setExpandedIndex(expanded ? null : index)
                        }
                      >
                        {entryTitle(entry)}
                      </button>
                      {entry.run_id != null && (
                        <span className="backend-chip">{String(entry.run_id)}</span>
                      )}
                      {entry.backend != null && (
                        <span className="backend-chip">{String(entry.backend)}</span>
                      )}
                    </div>
                    {typeof entry.detail === "string" && entry.detail && (
                      <div className="t-detail">{entry.detail}</div>
                    )}
                    {expanded && (
                      <div style={{ marginTop: "var(--space-2)", display: "grid", gap: "var(--space-2)" }}>
                        <div className="log-view" style={{ maxHeight: 200 }}>
                          {JSON.stringify(entry, null, 2)}
                        </div>
                        <button
                          className="btn"
                          style={{ justifySelf: "start", minHeight: 26 }}
                          onClick={() =>
                            navigator.clipboard.writeText(
                              JSON.stringify(entry, null, 2),
                            )
                          }
                        >
                          <ClipboardCopy size={13} aria-hidden /> Copy JSON
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Panel>
    </div>
  );
}
