/* Capacity: reported vs estimated vs unknown, made impossible to confuse.
   Charts follow design-system/pages/capacity.md: single-hue magnitude bars
   (dashed fill = estimate), status-coloured cycle history with text
   alternative, no fake precision, no streaming visuals. */
import { useState } from "react";
import { useCapacity, useProject } from "../state/queries";
import { decisionStatus } from "../lib/status";
import {
  formatDuration,
  formatExactOrEstimate,
  formatTime,
  formatTokens,
} from "../lib/format";
import {
  BackendChip,
  Countdown,
  Panel,
  Skeleton,
  StatusChip,
} from "../components/ui";

export function Capacity() {
  const { data, isLoading } = useCapacity();
  const { data: project } = useProject();
  const [showTable, setShowTable] = useState(false);

  if (isLoading || !data)
    return (
      <div className="stack">
        <h1 className="visually-hidden">Capacity</h1>
        <Panel title="Loading">
          <Skeleton />
        </Panel>
      </div>
    );

  const decision = data.decision;
  const estimate = data.estimate;
  const maxTokens = Math.max(
    1,
    ...data.per_backend.map(
      (backend) =>
        backend.tokens_last_day.input +
        backend.tokens_last_day.output +
        backend.tokens_last_day.reasoning,
    ),
  );
  const cycles = data.recent_cycles;
  const maxCycleTokens = Math.max(
    1,
    ...cycles.map((cycle) => Number(cycle.total_tokens) || 0),
  );

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Capacity</h1>
        <span className="page-sub">{data.note}</span>
      </div>

      <div className="grid cols-2">
        <Panel title="Next-cycle decision">
          {decision ? (
            <div
              className={`decision-card tone-${decisionStatus(decision.decision).tone}`}
              style={{ border: "none", padding: 0 }}
            >
              <span className="decision-verb">
                {decision.decision.replace("_", " ")}
              </span>
              <p>{decision.reason}</p>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                <span
                  className={`confidence-chip ${decision.confidence}`}
                  title="reported = provider said so; estimated = local history; unknown = no data"
                >
                  {decision.confidence}
                </span>
                {decision.selected_backend && (
                  <BackendChip name={decision.selected_backend} />
                )}
                {decision.fallback_candidates.length > 0 && (
                  <span className="control-hint">
                    fallbacks: {decision.fallback_candidates.join(" → ")}
                  </span>
                )}
                {decision.wait_until && (
                  <Countdown
                    until={decision.wait_until}
                    label="Capacity wait countdown"
                  />
                )}
              </div>
            </div>
          ) : (
            <p className="control-hint">
              No routing configured — choose a primary backend first.
            </p>
          )}
        </Panel>

        <Panel title="Next-cycle estimate" sub="never a provider quota">
          {estimate ? (
            <dl className="kv">
              <dt>task</dt>
              <dd className="mono">{data.next_task ?? "—"}</dd>
              <dt>estimated cycle</dt>
              <dd>{formatTokens(estimate.estimated_cycle_tokens)} tokens</dd>
              <dt>highest similar</dt>
              <dd>
                {estimate.highest_recent_cycle_tokens
                  ? `${formatTokens(estimate.highest_recent_cycle_tokens)} tokens (${estimate.history_samples} samples)`
                  : "no history yet"}
              </dd>
              <dt>safety multiplier</dt>
              <dd>×{estimate.safety_multiplier}</dd>
              <dt>required</dt>
              <dd>
                <strong>
                  {formatTokens(estimate.required_capacity_tokens)} tokens
                </strong>{" "}
                <span className="confidence-chip estimated">estimate</span>
              </dd>
              <dt>moving average</dt>
              <dd>
                {data.moving_average_cycle_tokens
                  ? `${formatTokens(data.moving_average_cycle_tokens)} tokens/cycle`
                  : "no completed cycles"}
              </dd>
            </dl>
          ) : (
            <p className="control-hint">Configure routing to see estimates.</p>
          )}
        </Panel>
      </div>

      <Panel
        title="Usage by backend"
        sub="last 24 hours, local ledger"
        actions={
          <button
            className="btn"
            style={{ minHeight: 26 }}
            onClick={() => setShowTable(!showTable)}
            aria-pressed={showTable}
          >
            {showTable ? "chart view" : "table view"}
          </button>
        }
      >
        {data.per_backend.length === 0 ? (
          <p className="control-hint">No backends configured.</p>
        ) : showTable ? (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Backend</th>
                  <th scope="col" className="num">Calls 1h</th>
                  <th scope="col" className="num">Calls 24h</th>
                  <th scope="col" className="num">Input</th>
                  <th scope="col" className="num">Cached</th>
                  <th scope="col" className="num">Output</th>
                  <th scope="col" className="num">Reasoning</th>
                  <th scope="col">Confidence</th>
                  <th scope="col">Breaker</th>
                </tr>
              </thead>
              <tbody>
                {data.per_backend.map((backend) => (
                  <tr key={backend.name}>
                    <td className="mono strong">{backend.name}</td>
                    <td className="num">{backend.calls_last_hour}</td>
                    <td className="num">{backend.calls_last_day}</td>
                    <td className="num">
                      {formatExactOrEstimate(
                        backend.tokens_last_day.input,
                        backend.tokens_estimated,
                      )}
                    </td>
                    <td className="num">
                      {formatExactOrEstimate(
                        backend.tokens_last_day.cached,
                        backend.tokens_estimated,
                      )}
                    </td>
                    <td className="num">
                      {formatExactOrEstimate(
                        backend.tokens_last_day.output,
                        backend.tokens_estimated,
                      )}
                    </td>
                    <td className="num">
                      {backend.tokens_last_day.reasoning || "—"}
                    </td>
                    <td>
                      <span
                        className={`confidence-chip ${backend.tokens_estimated ? "estimated" : "reported"}`}
                      >
                        {backend.tokens_estimated ? "estimate" : "reported"}
                      </span>
                    </td>
                    <td className="mono">{backend.breaker_state}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <figure className="chart-figure">
            <div className="stack" style={{ gap: "var(--space-2)" }}>
              {data.per_backend.map((backend) => {
                const total =
                  backend.tokens_last_day.input +
                  backend.tokens_last_day.output +
                  backend.tokens_last_day.reasoning;
                return (
                  <div
                    key={backend.name}
                    className={`hbar${backend.tokens_estimated ? " estimated" : ""}`}
                  >
                    <span className="mono">{backend.name}</span>
                    <span className="hbar-track">
                      <span
                        className="hbar-fill"
                        style={{ width: `${Math.round((total / maxTokens) * 100)}%` }}
                      />
                    </span>
                    <span>
                      {formatExactOrEstimate(total, backend.tokens_estimated)}{" "}
                      tok
                      {backend.tokens_estimated ? " (est)" : ""}
                    </span>
                  </div>
                );
              })}
            </div>
            <figcaption>
              Total tokens per backend over the last 24 hours. Dashed fill
              marks locally estimated figures; solid fill marks
              backend-reported ones.
            </figcaption>
          </figure>
        )}
      </Panel>

      <div className="grid cols-2">
        <Panel title="Cycle history" sub="last 20 cycles">
          {cycles.length === 0 ? (
            <p className="control-hint">
              No completed cycles yet. History sharpens the estimates.
            </p>
          ) : (
            <figure className="chart-figure">
              <div className="cycles-chart" role="img" aria-label={
                `Cycle token usage: ${cycles.map((c) => `${c.run_id} ${c.result} ${c.total_tokens} tokens`).join("; ")}`
              }>
                {cycles.map((cycle) => (
                  <span
                    key={cycle.run_id + cycle.timestamp}
                    className={`cycle-bar tone-${
                      cycle.result === "success"
                        ? "ok"
                        : cycle.result === "rate_limit" ||
                            cycle.result === "usage_limit"
                          ? "warn"
                          : "fail"
                    }`}
                    style={{
                      height: `${Math.max(
                        8,
                        Math.round(
                          ((Number(cycle.total_tokens) || 0) / maxCycleTokens) *
                            100,
                        ),
                      )}%`,
                    }}
                    title={`${cycle.run_id} · ${cycle.result} · ${formatTokens(
                      Number(cycle.total_tokens) || 0,
                    )} tokens · ${formatDuration(Number(cycle.duration_seconds) || 0)}`}
                  />
                ))}
              </div>
              <figcaption>
                Bar height = total tokens per cycle (estimate). Green:
                success · amber: provider limit · red: failure. Hover a bar
                for run id and duration.
              </figcaption>
            </figure>
          )}
        </Panel>

        <Panel title="Limits & events">
          <div className="stack" style={{ gap: "var(--space-3)" }}>
            <div>
              <span className="eyebrow">self-imposed limits</span>
              {data.per_backend.every(
                (backend) =>
                  !backend.limit_reasons.length &&
                  Object.values(backend.limits ?? {}).every((v) => v == null),
              ) ? (
                <p className="control-hint">
                  None configured — the OS never invents provider limits.
                  Set local ceilings in Settings.
                </p>
              ) : (
                <ul style={{ margin: "4px 0 0", paddingLeft: "1.2em" }}>
                  {data.per_backend.flatMap((backend) =>
                    Object.entries(backend.limits ?? {})
                      .filter(([, value]) => value != null)
                      .map(([key, value]) => (
                        <li key={backend.name + key} className="mono">
                          {backend.name}.{key} = {String(value)}
                          {backend.remaining_under_limits != null &&
                            ` (${formatTokens(backend.remaining_under_limits)} left)`}
                        </li>
                      )),
                  )}
                </ul>
              )}
            </div>
            <div>
              <span className="eyebrow">rate/usage-limit events</span>
              {data.limit_events.length === 0 ? (
                <p className="control-hint">None recorded.</p>
              ) : (
                <ul style={{ margin: "4px 0 0", paddingLeft: "1.2em" }}>
                  {data.limit_events.slice(-6).map((event, i) => (
                    <li key={i} className="mono">
                      {formatTime(String(event.ts))} ·{" "}
                      {String(event.backend ?? "")}{" "}
                      {String(event.kind ?? event.decision ?? "")}
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div>
              <span className="eyebrow">predicted recovery</span>
              {data.per_backend.filter((backend) => backend.unavailable_until)
                .length === 0 ? (
                <p className="control-hint">
                  All backends currently routable
                  {project?.scheduler.state === "cooling"
                    ? " (scheduler is cooling, not the backends)"
                    : ""}
                  .
                </p>
              ) : (
                data.per_backend
                  .filter((backend) => backend.unavailable_until)
                  .map((backend) => (
                    <p key={backend.name} style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      <BackendChip name={backend.name} />
                      <Countdown
                        until={backend.unavailable_until}
                        label={`${backend.name} recovery countdown`}
                      />
                      <span className="control-hint">
                        {formatTime(backend.unavailable_until)}
                      </span>
                    </p>
                  ))
              )}
            </div>
          </div>
        </Panel>
      </div>

      <Panel title="Recent cycles" flush>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Run</th>
                <th scope="col">Backend</th>
                <th scope="col">Task</th>
                <th scope="col" className="num">Tokens (est)</th>
                <th scope="col" className="num">Duration</th>
                <th scope="col">Result</th>
              </tr>
            </thead>
            <tbody>
              {cycles.length === 0 && (
                <tr>
                  <td colSpan={7} className="control-hint">
                    no cycles yet
                  </td>
                </tr>
              )}
              {[...cycles].reverse().map((cycle) => (
                <tr
                  key={cycle.run_id + cycle.timestamp}
                  className={
                    cycle.result === "success"
                      ? undefined
                      : cycle.result === "failure"
                        ? "failure"
                        : "attention"
                  }
                >
                  <td>{formatTime(cycle.timestamp)}</td>
                  <td className="mono strong">{cycle.run_id}</td>
                  <td className="mono">{cycle.backend}</td>
                  <td className="mono">{cycle.skill}</td>
                  <td className="num">
                    {formatTokens(Number(cycle.total_tokens) || 0)}
                  </td>
                  <td className="num">
                    {formatDuration(Number(cycle.duration_seconds) || 0)}
                  </td>
                  <td>
                    <StatusChip
                      outline
                      status={
                        cycle.result === "success"
                          ? { tone: "ok", label: "Success" }
                          : cycle.result === "rate_limit" ||
                              cycle.result === "usage_limit"
                            ? { tone: "warn", label: cycle.result }
                            : { tone: "fail", label: cycle.result }
                      }
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}
