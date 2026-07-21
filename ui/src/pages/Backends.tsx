/* Backends: detection, auth (as reported by each CLI — never credential
   files), breaker states, smoke tests (explicit cost confirmation), and
   routing assignment. `auth unknown` is never shown as usable. */
import { useState } from "react";
import { FlaskConical, RefreshCw, RotateCcw, Server } from "lucide-react";
import {
  useBackends,
  useBackendsRefresh,
  useResetBreaker,
  useSaveSettings,
  useSettings,
  useSmokeTest,
} from "../state/queries";
import { useLive } from "../state/events";
import { authStatus, breakerStatus } from "../lib/status";
import { formatTime } from "../lib/format";
import type { Backend } from "../lib/types";
import {
  BackendChip,
  ConfirmDialog,
  Countdown,
  EmptyState,
  Panel,
  Skeleton,
  StatusChip,
  Working,
} from "../components/ui";

function BackendRow({
  backend,
  onSmoke,
  onReset,
  onMakePrimary,
  savingPrimary,
}: {
  backend: Backend;
  onSmoke: () => void;
  onReset: () => void;
  onMakePrimary: () => void;
  savingPrimary: boolean;
}) {
  const breaker = breakerStatus(backend.breaker_state);
  const auth = authStatus(backend.auth);
  const detectable = backend.classification !== "api";
  return (
    <div className="backend-row">
      <div className="backend-id">
        <span className="backend-name">{backend.name}</span>
        <span className="eyebrow">{backend.classification}</span>
        <span style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          {backend.is_primary && (
            <StatusChip outline status={{ tone: "run", label: "Primary" }} />
          )}
          {backend.in_fallbacks && (
            <StatusChip outline status={{ tone: "idle", label: "Fallback" }} />
          )}
        </span>
      </div>
      <div className="backend-facts">
        <span className="fact">
          <span className="eyebrow">detected</span>
          {detectable ? (
            <StatusChip
              outline
              status={
                backend.detected
                  ? {
                      tone: "ok",
                      label: (backend.version ?? "Installed").slice(0, 22),
                    }
                  : { tone: "fail", label: "Not detected" }
              }
              title={backend.version ?? undefined}
            />
          ) : (
            <span>n/a (API)</span>
          )}
        </span>
        <span className="fact">
          <span className="eyebrow">auth</span>
          <StatusChip outline status={auth} title="As reported by the CLI itself; credential files are never read" />
        </span>
        <span className="fact">
          <span className="eyebrow">circuit breaker</span>
          <span style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            <StatusChip outline status={breaker} />
            {backend.unavailable_until && (
              <Countdown
                until={backend.unavailable_until}
                label={`${backend.name} retry countdown`}
              />
            )}
            {backend.recoverable_now && (
              <StatusChip
                outline
                status={{ tone: "idle", label: "Recoverable" }}
                title="A successful smoke test would likely clear this breaker now"
              />
            )}
          </span>
        </span>
        <span className="fact">
          <span className="eyebrow">smoke test</span>
          <span>
            {backend.smoke_test_passed === true
              ? "passed"
              : backend.smoke_test_passed === false
                ? "failed"
                : "not run"}
          </span>
        </span>
        <span className="fact">
          <span className="eyebrow">model</span>
          <span className="mono">
            {backend.model ??
              (backend.models.length > 0
                ? `${backend.models.length} available`
                : "—")}
          </span>
        </span>
        <span className="fact">
          <span className="eyebrow">last ok / failure</span>
          <span>
            {formatTime(backend.last_ok)}
            {backend.last_failure_kind
              ? ` · ${backend.last_failure_kind} ×${backend.consecutive_failures}`
              : ""}
          </span>
        </span>
        <span className="fact">
          <span className="eyebrow">serves</span>
          <span style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
            {backend.roles.length > 0
              ? backend.roles.map((role) => (
                  <span key={role} className="backend-chip">
                    {role}
                  </span>
                ))
              : "—"}
          </span>
        </span>
        {backend.api_key_env && (
          <span className="fact">
            <span className="eyebrow">key env</span>
            <span className="mono">{backend.api_key_env}</span>
          </span>
        )}
      </div>
      <div className="backend-actions">
        {!backend.is_primary && (
          <button
            className="btn"
            onClick={onMakePrimary}
            disabled={savingPrimary || backend.usable === false}
            title={
              backend.usable === false
                ? "not usable: fix detection/auth first"
                : "make this the primary backend for routing"
            }
          >
            Set as primary
          </button>
        )}
        {backend.classification !== "api" && (
          <button
            className="btn"
            onClick={onSmoke}
            disabled={!backend.detected}
            title={
              backend.detected
                ? "runs one real, tiny invocation — consumes allowance"
                : "backend not detected"
            }
          >
            <FlaskConical size={14} aria-hidden /> Smoke test
          </button>
        )}
        {backend.breaker_state !== "available" && (
          <button className="btn" onClick={onReset}>
            <RotateCcw size={14} aria-hidden /> Reset breaker
          </button>
        )}
      </div>
    </div>
  );
}

export function Backends() {
  const { data, isLoading } = useBackends();
  const { data: settings } = useSettings();
  const refresh = useBackendsRefresh();
  const smoke = useSmokeTest();
  const reset = useResetBreaker();
  const saveSettings = useSaveSettings();
  const { pushToast } = useLive();
  const [confirmSmoke, setConfirmSmoke] = useState<string | null>(null);
  const [confirmReset, setConfirmReset] = useState<string | null>(null);

  if (isLoading)
    return (
      <div className="stack">
        <h1 className="visually-hidden">Backends</h1>
        <Panel title="Loading">
          <Skeleton />
        </Panel>
      </div>
    );

  const backends = data?.backends ?? [];
  const makePrimary = (name: string) => {
    const others = backends
      .filter((b) => b.name !== name && (b.is_primary || b.in_fallbacks))
      .map((b) => b.name);
    saveSettings.mutate(
      {
        routing: {
          mode: settings?.routing.mode ?? "simple",
          primary: name,
          fallbacks: others,
        },
      },
      {
        onSuccess: () =>
          pushToast({ tone: "ok", title: `${name} is now primary` }),
        onError: (error) =>
          pushToast({
            tone: "fail",
            title: "Could not update routing",
            detail: error.message,
            sticky: true,
          }),
      },
    );
  };

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Backends</h1>
        <span className="page-sub">
          auth stays with each CLI — credential files are never read
        </span>
        <span style={{ marginLeft: "auto" }}>
          <button
            className="btn"
            onClick={() =>
              refresh.mutate(undefined, {
                onError: (error) =>
                  pushToast({
                    tone: "fail",
                    title: "Refresh failed",
                    detail: error.message,
                  }),
              })
            }
            disabled={refresh.isPending}
          >
            {refresh.isPending ? (
              <Working label="Detecting…" />
            ) : (
              <>
                <RefreshCw size={14} aria-hidden /> Refresh detection
              </>
            )}
          </button>
        </span>
      </div>

      <Panel flush>
        {backends.length === 0 ? (
          <EmptyState
            icon={<Server size={26} aria-hidden />}
            title="No backends configured"
          >
            Run <span className="mono">py .agentic/run setup</span> to detect
            installed CLIs and Ollama, or configure an API backend in
            <span className="mono"> .agentic/config.yaml</span>.
          </EmptyState>
        ) : (
          backends.map((backend) => (
            <BackendRow
              key={backend.name}
              backend={backend}
              onSmoke={() => setConfirmSmoke(backend.name)}
              onReset={() => setConfirmReset(backend.name)}
              onMakePrimary={() => makePrimary(backend.name)}
              savingPrimary={saveSettings.isPending}
            />
          ))
        )}
      </Panel>

      <Panel title="Setup guidance">
        <ul style={{ margin: 0, paddingLeft: "1.2em", color: "var(--text-secondary)" }}>
          <li>
            CLIs (Codex, Claude Code, Qwen) authenticate through their own{" "}
            <span className="mono">login</span> flows in your terminal; the
            dashboard only reads the status they report.
          </li>
          <li>
            An installed but failing CLI is treated as unusable — never
            routed to.
          </li>
          <li>
            Ollama serves local models at no cost; select its model in
            Settings after <span className="mono">ollama pull</span>.
          </li>
          <li>
            API backends are optional and stay disabled until their key
            environment variable is set. No key, no nagging.
          </li>
        </ul>
      </Panel>

      <ConfirmDialog
        open={confirmSmoke !== null}
        title={`Smoke test ${confirmSmoke ?? ""}`}
        body={
          <>
            <p>
              This runs one small <em>real</em> invocation on{" "}
              <span className="mono">{confirmSmoke}</span>.
            </p>
            <p>
              <strong>
                It consumes real subscription allowance or API cost.
              </strong>
            </p>
          </>
        }
        confirmLabel="Run smoke test"
        working={smoke.isPending}
        onCancel={() => setConfirmSmoke(null)}
        onConfirm={() => {
          if (confirmSmoke)
            smoke.mutate(confirmSmoke, {
              onSuccess: () =>
                pushToast({
                  tone: "run",
                  title: `Smoke test started on ${confirmSmoke}`,
                }),
              onError: (error) =>
                pushToast({
                  tone: "fail",
                  title: "Smoke test not started",
                  detail: error.message,
                  sticky: true,
                }),
            });
          setConfirmSmoke(null);
        }}
      />

      <ConfirmDialog
        open={confirmReset !== null}
        title={`Reset circuit breaker for ${confirmReset ?? ""}`}
        danger
        body={
          <p>
            The breaker opened because of observed failures. Resetting
            discards that evidence and the next cycle may route straight
            back to a failing backend. Reset only if you fixed the cause
            (e.g. logged in again).
          </p>
        }
        confirmLabel="Reset breaker"
        working={reset.isPending}
        onCancel={() => setConfirmReset(null)}
        onConfirm={() => {
          if (confirmReset)
            reset.mutate(confirmReset, {
              onSuccess: () =>
                pushToast({ tone: "ok", title: `Breaker reset: ${confirmReset}` }),
              onError: (error) =>
                pushToast({
                  tone: "fail",
                  title: "Reset failed",
                  detail: error.message,
                  sticky: true,
                }),
            });
          setConfirmReset(null);
        }}
      />

      <p className="control-hint">
        <BackendChip name="qwen" /> and other CLIs appear here only when
        configured; a missing command is shown as not detected and is never
        included in routing automatically.
      </p>
    </div>
  );
}
