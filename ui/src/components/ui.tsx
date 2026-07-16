/* Shared primitives: status chip, panel, meter, countdown, key-value list,
   empty/skeleton states, confirmation dialog, drawer, toasts. */
import {
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  AlertTriangle,
  CheckCircle2,
  CircleDot,
  Clock,
  HelpCircle,
  Loader2,
  OctagonAlert,
  PauseCircle,
  X,
  XCircle,
} from "lucide-react";
import type { StatusInfo, Tone } from "../lib/status";
import { formatCountdown, secondsUntil } from "../lib/format";
import { useLive } from "../state/events";

const TONE_ICON: Record<Tone, typeof CheckCircle2> = {
  ok: CheckCircle2,
  run: CircleDot,
  cool: Clock,
  warn: AlertTriangle,
  block: OctagonAlert,
  fail: XCircle,
  idle: PauseCircle,
};

export function StatusChip({
  status,
  outline,
  pulse,
  title,
}: {
  status: StatusInfo;
  outline?: boolean;
  pulse?: boolean;
  title?: string;
}) {
  const Icon = TONE_ICON[status.tone] ?? HelpCircle;
  return (
    <span
      className={`status-chip tone-${status.tone}${outline ? " outline" : ""}`}
      title={title ?? status.description}
    >
      <Icon size={12} aria-hidden className={pulse ? "pulse" : undefined} />
      {status.label}
    </span>
  );
}

export function BackendChip({ name }: { name: string | null | undefined }) {
  if (!name) return <span className="backend-chip">—</span>;
  return <span className="backend-chip">{name}</span>;
}

export function Panel({
  title,
  sub,
  actions,
  children,
  flush,
  as: Heading = "h2",
}: {
  title?: ReactNode;
  sub?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
  as?: "h2" | "h3";
}) {
  return (
    <section className="panel">
      {title !== undefined && (
        <div className="panel-head">
          <Heading className="panel-title" style={{ font: "var(--type-h2)" }}>
            {title}
          </Heading>
          {sub && <span className="panel-sub">{sub}</span>}
          {actions && <span style={{ marginLeft: "auto" }}>{actions}</span>}
        </div>
      )}
      <div className={`panel-body${flush ? " flush" : ""}`}>{children}</div>
    </section>
  );
}

export function Meter({
  label,
  done,
  total,
  tone,
}: {
  label: string;
  done: number;
  total: number;
  tone?: "ok";
}) {
  const pct = total ? Math.round((done / total) * 100) : 0;
  return (
    <div
      className={`meter${tone ? ` tone-${tone}` : ""}`}
      role="progressbar"
      aria-valuenow={done}
      aria-valuemin={0}
      aria-valuemax={total}
      aria-label={`${label}: ${done} of ${total}`}
    >
      <span className="meter-label">{label}</span>
      <span className="meter-track">
        <span className="meter-fill" style={{ width: `${pct}%` }} />
      </span>
      <span className="meter-value">
        {done}/{total}
      </span>
    </div>
  );
}

export function Countdown({
  until,
  label,
  onElapsed,
}: {
  until: string | null | undefined;
  label: string;
  onElapsed?: () => void;
}) {
  const [remaining, setRemaining] = useState(() => secondsUntil(until));
  const fired = useRef(false);
  useEffect(() => {
    fired.current = false;
    setRemaining(secondsUntil(until));
    if (!until) return;
    const timer = window.setInterval(() => {
      const left = secondsUntil(until);
      setRemaining(left);
      if (left !== null && left <= 0 && !fired.current) {
        fired.current = true;
        onElapsed?.();
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [until, onElapsed]);
  if (remaining === null) return null;
  return (
    <span className="countdown" aria-live="off" aria-label={label}>
      <Clock size={12} aria-hidden />
      {remaining <= 0 ? "due now" : formatCountdown(remaining)}
    </span>
  );
}

export function KV({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="kv">
      {items.map(([key, value]) => (
        <div key={key} style={{ display: "contents" }}>
          <dt>{key}</dt>
          <dd>{value ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

export function EmptyState({
  icon,
  title,
  children,
  action,
}: {
  icon?: ReactNode;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      {icon}
      <p className="empty-title">{title}</p>
      {children && <div>{children}</div>}
      {action}
    </div>
  );
}

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="stack" aria-hidden style={{ gap: "var(--space-2)" }}>
      {Array.from({ length: lines }, (_, i) => (
        <div
          className="skeleton"
          key={i}
          style={{ width: `${90 - i * 12}%` }}
        />
      ))}
    </div>
  );
}

export function Working({ label }: { label: string }) {
  return (
    <>
      <Loader2 size={14} className="spin" aria-hidden /> {label}
    </>
  );
}

/* -- accessible confirmation dialog ------------------------------------------ */

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  danger,
  onConfirm,
  onCancel,
  working,
}: {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  working?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const node = ref.current;
    const focusables = () =>
      node?.querySelectorAll<HTMLElement>(
        "button, [href], input, select, textarea",
      ) ?? [];
    (focusables()[0] as HTMLElement | undefined)?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
      if (event.key === "Tab") {
        const items = Array.from(focusables());
        if (items.length === 0) return;
        const first = items[0];
        const last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus();
    };
  }, [open, onCancel]);
  if (!open) return null;
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onCancel()}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        ref={ref}
      >
        <div className="dialog-head" id={titleId}>
          {title}
        </div>
        <div className="dialog-body">{body}</div>
        <div className="dialog-foot">
          <button className="btn" onClick={onCancel}>
            Cancel
          </button>
          <button
            className={`btn ${danger ? "danger" : "primary"}`}
            onClick={onConfirm}
            disabled={working}
          >
            {working ? <Working label={confirmLabel} /> : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

/* -- drawer -------------------------------------------------------------------- */

export function Drawer({
  open,
  title,
  onClose,
  children,
}: {
  open: boolean;
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
}) {
  const titleId = useId();
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>("button")?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus();
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div
      className="drawer"
      role="dialog"
      aria-modal="false"
      aria-labelledby={titleId}
      ref={ref}
    >
      <div className="drawer-head">
        <span id={titleId} style={{ font: "var(--type-h2)" }}>
          {title}
        </span>
        <button className="btn" onClick={onClose} aria-label="Close details">
          <X size={14} aria-hidden />
        </button>
      </div>
      <div className="drawer-body">{children}</div>
    </div>
  );
}

/* -- toasts ---------------------------------------------------------------------- */

export function Toasts() {
  const { toasts, dismissToast } = useLive();
  return (
    <div className="toasts" aria-live="polite" aria-label="Notifications">
      {toasts.map((toast) => (
        <div key={toast.id} className={`toast tone-${toast.tone}`} role="status">
          <div className="toast-body">
            <div className="toast-title">{toast.title}</div>
            {toast.detail && <div className="toast-detail">{toast.detail}</div>}
          </div>
          <button
            onClick={() => dismissToast(toast.id)}
            aria-label="Dismiss notification"
          >
            <X size={14} aria-hidden />
          </button>
        </div>
      ))}
    </div>
  );
}
