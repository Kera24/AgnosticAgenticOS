/* Verification: the deterministic gate's evidence. Zero configured checks
   is shown as BLOCKING (never a soft warning); baseline failures are
   distinguished from new regressions; full logs open via the validated
   backend endpoint only. */
import { useState } from "react";
import { FileSearch, ShieldCheck } from "lucide-react";
import { useRunLog, useVerification } from "../state/queries";
import { formatTime } from "../lib/format";
import {
  Drawer,
  EmptyState,
  KV,
  Panel,
  Skeleton,
  StatusChip,
} from "../components/ui";

export function Verification() {
  const { data, isLoading } = useVerification();
  const [logTarget, setLogTarget] = useState<{
    run: string;
    name: string;
  } | null>(null);
  const log = useRunLog(logTarget?.run ?? null, logTarget?.name ?? null);

  if (isLoading || !data)
    return (
      <div className="stack">
        <h1 className="visually-hidden">Verification</h1>
        <Panel title="Loading">
          <Skeleton />
        </Panel>
      </div>
    );

  const qaVerdict = data.qa?.verdict as string | undefined;
  const secVerdict = data.security?.verdict as string | undefined;
  const audit = data.final_audit;

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Verification</h1>
        <span className="page-sub">
          deterministic checks are the final vote — no model overrides them
        </span>
      </div>

      {!data.configured && (
        <Panel>
          <EmptyState
            icon={<ShieldCheck size={26} aria-hidden />}
            title="No deterministic checks configured — this blocks all cycles"
          >
            A repository without real checks can never pass the gate. Add
            tests (pytest/npm/cargo/go are auto-detected) or configure{" "}
            <span className="mono">verification.commands</span> in{" "}
            <span className="mono">.agentic/config.yaml</span>.
          </EmptyState>
        </Panel>
      )}

      {data.configured && (
        <Panel
          title="Configured checks"
          sub={data.auto_detected ? "auto-detected from the repository" : "pinned in configuration"}
          flush
        >
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Check</th>
                  <th scope="col">Command</th>
                  <th scope="col">Mandatory</th>
                  <th scope="col">Baseline</th>
                </tr>
              </thead>
              <tbody>
                {data.commands.map((command) => {
                  const baselinePassed =
                    data.baseline?.checks?.[command.name];
                  return (
                    <tr key={command.name}>
                      <td className="mono strong">{command.name}</td>
                      <td className="mono">{command.command}</td>
                      <td>
                        {command.mandatory ? (
                          <StatusChip
                            outline
                            status={{ tone: "run", label: "Mandatory" }}
                          />
                        ) : (
                          <StatusChip
                            outline
                            status={{ tone: "idle", label: "Optional" }}
                          />
                        )}
                      </td>
                      <td>
                        {baselinePassed === undefined ? (
                          <span className="control-hint">not recorded</span>
                        ) : baselinePassed ? (
                          <StatusChip outline status={{ tone: "ok", label: "Passing" }} />
                        ) : (
                          <StatusChip
                            outline
                            status={{ tone: "warn", label: "Known failing" }}
                            title="Pre-existing failure: tolerated but reported; new failures are regressions"
                          />
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Panel>
      )}

      <Panel
        title="Latest gate run"
        sub={
          data.latest.run
            ? `${data.latest.run}${data.latest.attempt_dir ? ` · ${data.latest.attempt_dir}` : ""}`
            : "no recorded runs yet"
        }
      >
        {data.latest.results.length === 0 ? (
          <p className="control-hint">
            Check logs appear here after the first cycle runs the gate.
          </p>
        ) : (
          <div className="stack" style={{ gap: "var(--space-3)" }}>
            {data.latest.results.map((check) => (
              <div
                key={check.name}
                className="panel"
                style={{
                  boxShadow: check.new_regression
                    ? "inset 2px 0 0 var(--status-fail)"
                    : check.passed
                      ? "inset 2px 0 0 var(--status-ok)"
                      : "inset 2px 0 0 var(--status-warn)",
                }}
              >
                <div className="panel-head" style={{ borderBottom: "none" }}>
                  <span className="mono strong">{check.name}</span>
                  <StatusChip
                    outline
                    status={
                      check.passed
                        ? { tone: "ok", label: "Passed" }
                        : check.known_baseline_failure
                          ? { tone: "warn", label: "Known baseline failure" }
                          : { tone: "fail", label: "New regression" }
                    }
                  />
                  <span className="control-hint">
                    exit {check.exit_code ?? "?"}
                  </span>
                  <span style={{ marginLeft: "auto" }}>
                    <button
                      className="btn"
                      style={{ minHeight: 26 }}
                      onClick={() =>
                        setLogTarget({
                          run: check.run ?? data.latest.run ?? "",
                          name: check.name,
                        })
                      }
                    >
                      <FileSearch size={13} aria-hidden /> Full log
                    </button>
                  </span>
                </div>
                {!check.passed && (
                  <div className="panel-body" style={{ paddingTop: 0 }}>
                    <div className="log-view" style={{ maxHeight: 160 }}>
                      {check.excerpt}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Panel>

      <div className="grid cols-2">
        <Panel title="QA verdict">
          {data.qa ? (
            <KV
              items={[
                [
                  "Verdict",
                  <StatusChip
                    key="v"
                    status={
                      qaVerdict === "pass"
                        ? { tone: "ok", label: "Pass" }
                        : qaVerdict === "uncertain"
                          ? { tone: "warn", label: "Uncertain" }
                          : { tone: "fail", label: qaVerdict ?? "unknown" }
                    }
                  />,
                ],
                ["Cycle", String(data.qa.run_id ?? "—")],
                ["At", formatTime(String(data.qa.ts))],
              ]}
            />
          ) : (
            <p className="control-hint">No QA review recorded yet.</p>
          )}
        </Panel>
        <Panel title="Security verdict" sub="conditional review">
          {data.security ? (
            <KV
              items={[
                [
                  "Verdict",
                  <StatusChip
                    key="v"
                    status={
                      secVerdict === "pass"
                        ? { tone: "ok", label: "Pass" }
                        : secVerdict === "human_review_required"
                          ? { tone: "block", label: "Human review required" }
                          : { tone: "fail", label: secVerdict ?? "unknown" }
                    }
                  />,
                ],
                ["Cycle", String(data.security.run_id ?? "—")],
                ["At", formatTime(String(data.security.ts))],
              ]}
            />
          ) : (
            <p className="control-hint">
              Not triggered — it runs only for security-relevant changes.
            </p>
          )}
        </Panel>
      </div>

      <Panel title="Final audit evidence">
        {!audit ? (
          <p className="control-hint">
            The final audit runs when the backlog completes (or on demand
            from Build Control) and records evidence here.
          </p>
        ) : (
          <div className="stack" style={{ gap: "var(--space-3)" }}>
            <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
              <StatusChip
                status={
                  audit.complete
                    ? { tone: "ok", label: "Complete" }
                    : { tone: "fail", label: "Audit failed" }
                }
              />
              <span className="control-hint">
                {formatTime(audit.completed_at)} · branch{" "}
                <span className="mono">{audit.branch}</span>
              </span>
            </div>
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th scope="col">Evidence check</th>
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
            {audit.completion_criteria.length > 0 && (
              <div>
                <span className="eyebrow">acceptance criteria</span>
                <ul style={{ margin: "4px 0 0", paddingLeft: "1.2em" }}>
                  {audit.completion_criteria.map((criterion, i) => (
                    <li key={i}>{criterion}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </Panel>

      <Drawer
        open={logTarget !== null}
        title={
          <span className="mono">
            {logTarget?.run} / {logTarget?.name}.log
          </span>
        }
        onClose={() => setLogTarget(null)}
      >
        {log.isLoading ? (
          <Skeleton lines={6} />
        ) : log.isError ? (
          <p className="field-error">{log.error.message}</p>
        ) : (
          <div className="log-view" style={{ maxHeight: "70vh" }}>
            {log.data?.content}
          </div>
        )}
      </Drawer>
    </div>
  );
}
