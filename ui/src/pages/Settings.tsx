/* Settings: the safe machine-local configuration surface. Everything is
   validated server-side and written only to .agentic/config.machine.yaml.
   Secrets have no place here by construction. */
import { useEffect, useState } from "react";
import { useSaveSettings, useSettings } from "../state/queries";
import { useLive } from "../state/events";
import type { Settings as SettingsType } from "../lib/types";
import { Panel, Skeleton, Working } from "../components/ui";

type Draft = {
  interaction_mode: string;
  target: string;
  maximum: string;
  cool_ok: string;
  cool_fail: string;
  cool_min: string;
  cool_max: string;
  multiplier: string;
  window_enabled: boolean;
  window_start: string;
  window_stop: string;
  timezone: string;
  desktop: boolean;
  port: string;
  open_browser: boolean;
  theme: "dark" | "light";
  reduced_motion: "system" | "on" | "off";
  limits: Record<string, Record<string, string>>;
};

const LIMIT_FIELDS = [
  ["maximum_calls_per_hour", "calls/hour"],
  ["maximum_calls_per_day", "calls/day"],
  ["maximum_estimated_tokens_per_hour", "est. tokens/hour"],
  ["maximum_estimated_tokens_per_day", "est. tokens/day"],
] as const;

function fromSettings(settings: SettingsType): Draft {
  return {
    interaction_mode: settings.interaction.mode,
    target: String(settings.cycle.target_duration_minutes),
    maximum: String(settings.cycle.maximum_duration_minutes),
    cool_ok: String(settings.cooling.after_success_minutes),
    cool_fail: String(settings.cooling.after_failure_minutes),
    cool_min: String(settings.cooling.minimum_minutes),
    cool_max: String(settings.cooling.maximum_minutes),
    multiplier: String(settings.capacity.safety_multiplier),
    window_enabled: settings.operating_window.enabled,
    window_start: settings.operating_window.start,
    window_stop: settings.operating_window.stop,
    timezone: settings.operating_window.timezone,
    desktop: settings.notifications.desktop,
    port: String(settings.ui.port),
    open_browser: settings.ui.open_browser,
    theme: settings.ui.theme,
    reduced_motion:
      settings.ui.reduced_motion === "system"
        ? "system"
        : settings.ui.reduced_motion
          ? "on"
          : "off",
    limits: Object.fromEntries(
      settings.backends_configured.map((backend) => [
        backend,
        Object.fromEntries(
          LIMIT_FIELDS.map(([key]) => [
            key,
            settings.limits[backend]?.[key] != null
              ? String(settings.limits[backend][key])
              : "",
          ]),
        ),
      ]),
    ),
  };
}

export function Settings() {
  const { data: settings, isLoading } = useSettings();
  const save = useSaveSettings();
  const { pushToast } = useLive();
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (settings && draft === null) setDraft(fromSettings(settings));
  }, [settings, draft]);

  if (isLoading || !settings || !draft)
    return (
      <div className="stack">
        <h1 className="visually-hidden">Settings</h1>
        <Panel title="Loading">
          <Skeleton />
        </Panel>
      </div>
    );

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((current) => (current ? { ...current, [key]: value } : current));

  const submit = () => {
    setError(null);
    const limits: Record<string, Record<string, number | null>> = {};
    for (const [backend, entries] of Object.entries(draft.limits)) {
      limits[backend] = {};
      for (const [key, raw] of Object.entries(entries)) {
        limits[backend][key] = raw.trim() === "" ? null : Number(raw);
      }
    }
    save.mutate(
      {
        interaction: { mode: draft.interaction_mode },
        cycle: {
          target_duration_minutes: Number(draft.target),
          maximum_duration_minutes: Number(draft.maximum),
        },
        cooling: {
          after_success_minutes: Number(draft.cool_ok),
          after_failure_minutes: Number(draft.cool_fail),
          minimum_minutes: Number(draft.cool_min),
          maximum_minutes: Number(draft.cool_max),
        },
        capacity: { safety_multiplier: Number(draft.multiplier) },
        operating_window: {
          enabled: draft.window_enabled,
          start: draft.window_start,
          stop: draft.window_stop,
          timezone: draft.timezone,
        },
        notifications: { desktop: draft.desktop },
        ui: {
          port: Number(draft.port),
          open_browser: draft.open_browser,
          theme: draft.theme,
          reduced_motion:
            draft.reduced_motion === "system"
              ? "system"
              : draft.reduced_motion === "on",
        },
        limits,
      },
      {
        onSuccess: () => {
          pushToast({
            tone: "ok",
            title: "Settings saved",
            detail: "written to .agentic/config.machine.yaml",
          });
          setDraft(null); // re-hydrate from the saved server state
        },
        onError: (err) => setError(err.message),
      },
    );
  };

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Settings</h1>
        <span className="page-sub">
          machine-local only · never credentials
        </span>
      </div>

      <div className="grid cols-2">
        <Panel title="Interaction">
          <div className="stack" style={{ gap: "var(--space-3)" }}>
            <div className="field">
              <label htmlFor="s-mode">Interaction mode</label>
              <select
                id="s-mode"
                className="input"
                value={draft.interaction_mode}
                onChange={(e) => set("interaction_mode", e.target.value)}
              >
                <option value="completion_only">
                  completion only — notify when the project is done
                </option>
                <option value="milestone_review">
                  milestone review — notify at each milestone
                </option>
                <option value="cycle_review">
                  cycle review — notify after every cycle
                </option>
              </select>
            </div>
            <label className="check-row">
              <input
                type="checkbox"
                checked={draft.desktop}
                onChange={(e) => set("desktop", e.target.checked)}
              />
              Desktop notifications
            </label>
          </div>
        </Panel>

        <Panel title="Dashboard">
          <div className="stack" style={{ gap: "var(--space-3)" }}>
            <div className="field">
              <label htmlFor="s-port">Default port</label>
              <input
                id="s-port"
                className="input"
                inputMode="numeric"
                value={draft.port}
                onChange={(e) => set("port", e.target.value)}
                style={{ maxWidth: 140 }}
              />
              <span className="field-help">
                Applies on next start; a busy port scans upward safely.
              </span>
            </div>
            <label className="check-row">
              <input
                type="checkbox"
                checked={draft.open_browser}
                onChange={(e) => set("open_browser", e.target.checked)}
              />
              Open the browser on start
            </label>
            <div className="field">
              <label htmlFor="s-theme">Theme</label>
              <select
                id="s-theme"
                className="input"
                value={draft.theme}
                onChange={(e) => set("theme", e.target.value as "dark" | "light")}
                style={{ maxWidth: 200 }}
              >
                <option value="dark">dark (default)</option>
                <option value="light">light</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="s-motion">Reduced motion</label>
              <select
                id="s-motion"
                className="input"
                value={draft.reduced_motion}
                onChange={(e) =>
                  set(
                    "reduced_motion",
                    e.target.value as Draft["reduced_motion"],
                  )
                }
                style={{ maxWidth: 200 }}
              >
                <option value="system">follow system preference</option>
                <option value="on">always reduce motion</option>
                <option value="off">allow motion</option>
              </select>
            </div>
          </div>
        </Panel>

        <Panel title="Cycles & cooling">
          <div className="grid cols-2" style={{ gap: "var(--space-3)" }}>
            <div className="field">
              <label htmlFor="s-target">Cycle target (min)</label>
              <input id="s-target" className="input" inputMode="numeric"
                value={draft.target} onChange={(e) => set("target", e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="s-max">Cycle maximum (min)</label>
              <input id="s-max" className="input" inputMode="numeric"
                value={draft.maximum} onChange={(e) => set("maximum", e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="s-cok">Cooling after success (min)</label>
              <input id="s-cok" className="input" inputMode="numeric"
                value={draft.cool_ok} onChange={(e) => set("cool_ok", e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="s-cfail">Cooling after failure (min)</label>
              <input id="s-cfail" className="input" inputMode="numeric"
                value={draft.cool_fail} onChange={(e) => set("cool_fail", e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="s-cmin">Dynamic cooling minimum (min)</label>
              <input id="s-cmin" className="input" inputMode="numeric"
                value={draft.cool_min} onChange={(e) => set("cool_min", e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="s-cmax">Dynamic cooling maximum (min)</label>
              <input id="s-cmax" className="input" inputMode="numeric"
                value={draft.cool_max} onChange={(e) => set("cool_max", e.target.value)} />
            </div>
          </div>
        </Panel>

        <Panel title="Capacity">
          <div className="stack" style={{ gap: "var(--space-3)" }}>
            <div className="field">
              <label htmlFor="s-mult">Safety multiplier (1.0–3.0)</label>
              <input
                id="s-mult"
                className="input"
                inputMode="decimal"
                value={draft.multiplier}
                onChange={(e) => set("multiplier", e.target.value)}
                style={{ maxWidth: 140 }}
              />
              <span className="field-help">
                Estimated next-cycle need is multiplied by this reserve
                before a start decision.
              </span>
            </div>
            <div className="field">
              <span className="eyebrow" style={{ marginBottom: 4 }}>
                operating window
              </span>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={draft.window_enabled}
                  onChange={(e) => set("window_enabled", e.target.checked)}
                />
                Only run cycles between
              </label>
              <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
                <input
                  className="input"
                  aria-label="Window start (HH:MM)"
                  value={draft.window_start}
                  onChange={(e) => set("window_start", e.target.value)}
                  disabled={!draft.window_enabled}
                  style={{ maxWidth: 90 }}
                />
                <span aria-hidden>–</span>
                <input
                  className="input"
                  aria-label="Window stop (HH:MM)"
                  value={draft.window_stop}
                  onChange={(e) => set("window_stop", e.target.value)}
                  disabled={!draft.window_enabled}
                  style={{ maxWidth: 90 }}
                />
              </div>
            </div>
            <div className="field">
              <label htmlFor="s-tz">Timezone</label>
              <input
                id="s-tz"
                className="input"
                value={draft.timezone}
                onChange={(e) => set("timezone", e.target.value)}
                placeholder="e.g. Australia/Sydney"
                style={{ maxWidth: 240 }}
              />
            </div>
          </div>
        </Panel>
      </div>

      <Panel
        title="Self-imposed limits"
        sub="local ceilings you choose — provider limits are never invented"
        flush
      >
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Backend</th>
                {LIMIT_FIELDS.map(([key, label]) => (
                  <th scope="col" key={key} className="num">
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {settings.backends_configured.map((backend) => (
                <tr key={backend}>
                  <td className="mono strong">{backend}</td>
                  {LIMIT_FIELDS.map(([key, label]) => (
                    <td key={key} className="num">
                      <input
                        className="input"
                        style={{ maxWidth: 110, textAlign: "right" }}
                        inputMode="numeric"
                        aria-label={`${backend} ${label}`}
                        placeholder="none"
                        value={draft.limits[backend]?.[key] ?? ""}
                        onChange={(e) =>
                          setDraft((current) =>
                            current
                              ? {
                                  ...current,
                                  limits: {
                                    ...current.limits,
                                    [backend]: {
                                      ...current.limits[backend],
                                      [key]: e.target.value,
                                    },
                                  },
                                }
                              : current,
                          )
                        }
                      />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}

      <div style={{ display: "flex", gap: "var(--space-3)", alignItems: "center" }}>
        <button
          className="btn primary big"
          onClick={submit}
          disabled={save.isPending}
        >
          {save.isPending ? <Working label="Saving…" /> : "Save settings"}
        </button>
        <span className="control-hint">
          Saved to <span className="mono">.agentic/config.machine.yaml</span>{" "}
          (git-ignored). API keys live only in your environment; this file
          never holds secrets.
        </span>
      </div>
    </div>
  );
}
