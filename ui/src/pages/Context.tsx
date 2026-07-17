/* Context Intelligence: index status, package composition, retrieval
   savings (always labelled estimates), and code search. */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { formatTime, formatTokens } from "../lib/format";
import { Drawer, KV, Panel, Skeleton, StatusChip } from "../components/ui";

type PackageItem = {
  id: string;
  category: string;
  source_path: string | null;
  token_estimate: number | null;
  trust_level: string;
  reason?: string;
};

type PackageRecord = {
  package_id: string;
  role: string;
  created_at: string;
  token_budget: number;
  token_estimate: number;
  reserved_output_tokens: number;
  tokens_by_category?: Record<string, number>;
  estimated_savings_tokens?: number;
  candidate_total_tokens?: number;
  included: PackageItem[];
  omitted: { item: PackageItem; reason: string }[];
};

type ContextView = {
  code_intelligence: {
    provider: string;
    configured_provider: string;
    indexed: boolean;
    stale?: boolean;
    revision?: string | null;
    files_indexed?: number | null;
    indexed_at?: string | null;
    health: { ok: boolean; detail?: string | null };
    fallback_reason?: string | null;
  };
  packages: PackageRecord[];
  totals: { estimated_savings_tokens: number; token_estimate: number };
};

type SearchResult = {
  id: string;
  path: string;
  start_line: number;
  end_line: number;
  snippet: string;
  score: number;
};

export function Context() {
  const { data, isLoading } = useQuery({
    queryKey: ["context"],
    queryFn: () => api.get<ContextView>("/context"),
  });
  const [selected, setSelected] = useState<PackageRecord | null>(null);
  const [query, setQuery] = useState("");
  const [searched, setSearched] = useState("");
  const search = useQuery({
    queryKey: ["context-search", searched],
    queryFn: () =>
      api.get<{ provider: string; results: SearchResult[] }>(
        `/context/search?q=${encodeURIComponent(searched)}`,
      ),
    enabled: searched.length > 0,
  });

  if (isLoading || !data)
    return (
      <div className="stack">
        <h1 className="visually-hidden">Context intelligence</h1>
        <Panel title="Loading">
          <Skeleton />
        </Panel>
      </div>
    );

  const ci = data.code_intelligence;
  return (
    <div className="stack">
      <div className="page-head">
        <h1>Context Intelligence</h1>
        <span className="page-sub">
          every prompt is assembled by the Context Broker — token figures are
          local estimates
        </span>
      </div>

      <div className="grid cols-2">
        <Panel title="Code index">
          <div className="stack" style={{ gap: "var(--space-3)" }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <StatusChip
                status={
                  ci.health.ok
                    ? { tone: "ok", label: `${ci.provider} healthy` }
                    : { tone: "warn", label: `${ci.provider} degraded` }
                }
              />
              {ci.stale && (
                <StatusChip
                  outline
                  status={{ tone: "warn", label: "index stale" }}
                  title="repository moved past the indexed revision"
                />
              )}
            </div>
            <KV
              items={[
                ["configured", ci.configured_provider],
                ["active", ci.provider],
                ["files indexed", ci.files_indexed ?? "—"],
                ["revision", ci.revision ? String(ci.revision).slice(0, 10) : "—"],
                ["indexed at", ci.indexed_at ?? "—"],
                ["fallback reason", ci.fallback_reason ?? "none"],
              ]}
            />
          </div>
        </Panel>

        <Panel
          title="Retrieval savings"
          sub="estimated, never provider-reported"
        >
          <KV
            items={[
              [
                "packages recorded",
                data.packages.length,
              ],
              [
                "tokens sent (est)",
                `${formatTokens(data.totals.token_estimate)} tok`,
              ],
              [
                "tokens saved vs candidates (est)",
                `${formatTokens(data.totals.estimated_savings_tokens)} tok`,
              ],
            ]}
          />
          <p className="control-hint">
            Savings compare selected content against everything the sources
            offered before budgeting and deduplication.
          </p>
        </Panel>
      </div>

      <Panel title="Code search" sub="retrieval as the workers see it">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setSearched(query.trim());
          }}
          style={{ display: "flex", gap: 8 }}
        >
          <input
            className="input"
            style={{ flex: 1 }}
            placeholder="search tracked code…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Code search query"
          />
          <button className="btn primary" type="submit" disabled={!query.trim()}>
            Search
          </button>
        </form>
        {search.data && (
          <div className="stack" style={{ marginTop: "var(--space-3)" }}>
            {search.data.results.length === 0 && (
              <p className="control-hint">no matches</p>
            )}
            {search.data.results.map((r) => (
              <div key={r.id} className="panel" style={{ padding: 8 }}>
                <div className="mono strong">
                  {r.path}:{r.start_line}-{r.end_line}{" "}
                  <span className="control-hint">score {r.score}</span>
                </div>
                <pre className="code-block" style={{ maxHeight: 160, overflow: "auto" }}>
                  {r.snippet}
                </pre>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel title="Context packages" sub="newest first" flush>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Role</th>
                <th scope="col" className="num">Budget</th>
                <th scope="col" className="num">Sent (est)</th>
                <th scope="col" className="num">Saved (est)</th>
                <th scope="col" className="num">Omitted</th>
                <th scope="col">Composition</th>
              </tr>
            </thead>
            <tbody>
              {data.packages.length === 0 && (
                <tr>
                  <td colSpan={7} className="control-hint">
                    no packages yet — they appear as soon as any agent runs
                  </td>
                </tr>
              )}
              {data.packages.map((p) => (
                <tr
                  key={p.package_id}
                  onClick={() => setSelected(p)}
                  style={{ cursor: "pointer" }}
                >
                  <td>{formatTime(p.created_at)}</td>
                  <td className="mono strong">{p.role}</td>
                  <td className="num">{formatTokens(p.token_budget)}</td>
                  <td className="num">{formatTokens(p.token_estimate)}</td>
                  <td className="num">
                    {formatTokens(p.estimated_savings_tokens ?? 0)}
                  </td>
                  <td className="num">{p.omitted.length}</td>
                  <td className="mono">
                    {Object.entries(p.tokens_by_category ?? {})
                      .map(([cat, tok]) => `${cat}:${formatTokens(tok)}`)
                      .join(" ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Drawer
        open={selected !== null}
        title={selected ? `package ${selected.package_id}` : ""}
        onClose={() => setSelected(null)}
      >
        {selected && (
          <div className="stack">
            <KV
              items={[
                ["role", selected.role],
                ["budget", `${formatTokens(selected.token_budget)} tok`],
                ["sent (est)", `${formatTokens(selected.token_estimate)} tok`],
                [
                  "output reserve",
                  `${formatTokens(selected.reserved_output_tokens)} tok`,
                ],
              ]}
            />
            <span className="eyebrow">included</span>
            <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
              {selected.included.map((item) => (
                <li key={item.id} className="mono" style={{ fontSize: 12 }}>
                  [{item.category}] {item.source_path ?? "(os)"} ·{" "}
                  {formatTokens(item.token_estimate ?? 0)} tok
                  {item.trust_level === "untrusted" ? " · untrusted" : ""}
                  {item.reason ? ` — ${item.reason}` : ""}
                </li>
              ))}
            </ul>
            <span className="eyebrow">omitted</span>
            {selected.omitted.length === 0 ? (
              <p className="control-hint">nothing omitted</p>
            ) : (
              <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
                {selected.omitted.map((o, i) => (
                  <li key={i} className="mono" style={{ fontSize: 12 }}>
                    [{o.item.category}] {o.item.source_path ?? "(os)"} —{" "}
                    {o.reason}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </Drawer>
    </div>
  );
}
