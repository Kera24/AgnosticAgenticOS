/* Live events: one SSE connection drives cache invalidation, live activity,
   operation toasts and the connection banner. Reconnection uses the
   browser's native EventSource retry plus Last-Event-ID replay serverside. */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { eventsUrl } from "../lib/api";
import type { ActivityEntry, Operation } from "../lib/types";
import { keys } from "./queries";

export type ConnectionState = "connecting" | "open" | "lost";

export interface Toast {
  id: number;
  tone: "ok" | "fail" | "warn" | "run";
  title: string;
  detail?: string;
  sticky?: boolean;
}

interface LiveContextValue {
  connection: ConnectionState;
  toasts: Toast[];
  pushToast: (toast: Omit<Toast, "id">) => void;
  dismissToast: (id: number) => void;
  liveActivity: ActivityEntry[];
}

const LiveContext = createContext<LiveContextValue>({
  connection: "connecting",
  toasts: [],
  pushToast: () => {},
  dismissToast: () => {},
  liveActivity: [],
});

let toastSeq = 1;

function operationToast(op: Operation): Omit<Toast, "id"> | null {
  const kind = op.kind.replace("project.", "").replace("backend.", "");
  if (op.status === "running") {
    return { tone: "run", title: `${kind} started`, detail: op.detail };
  }
  if (op.status === "succeeded") {
    const status = (op.result as { status?: string } | null)?.status;
    const failureLike =
      status &&
      /fail|blocked|human_required|not_eligible|locked|no_project|audit_failed/.test(
        status,
      );
    return {
      tone: failureLike ? "warn" : "ok",
      title: `${kind}: ${status ?? "done"}`,
      detail: (op.result as { detail?: string } | null)?.detail,
      sticky: Boolean(failureLike),
    };
  }
  if (op.status === "failed") {
    return {
      tone: "fail",
      title: `${kind} failed`,
      detail: op.error ?? undefined,
      sticky: true,
    };
  }
  return null;
}

export function LiveProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [liveActivity, setLiveActivity] = useState<ActivityEntry[]>([]);
  const seenOps = useRef(new Map<string, string>());

  const pushToast = useCallback((toast: Omit<Toast, "id">) => {
    const id = toastSeq++;
    setToasts((prev) => [...prev.slice(-2), { ...toast, id }]);
    if (!toast.sticky) {
      window.setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== id));
      }, 6000);
    }
  }, []);

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  useEffect(() => {
    const source = new EventSource(eventsUrl);
    source.onopen = () => setConnection("open");
    source.onerror = () => setConnection("lost");

    source.addEventListener("state", (event) => {
      const data = JSON.parse((event as MessageEvent).data) as {
        changed?: string;
      };
      const changed = data.changed;
      qc.invalidateQueries({ queryKey: keys.project });
      if (changed === "backends") {
        qc.invalidateQueries({ queryKey: keys.backends });
        qc.invalidateQueries({ queryKey: keys.capacity });
      }
      if (changed === "progress" || changed === "backlog") {
        qc.invalidateQueries({ queryKey: keys.backlog });
        qc.invalidateQueries({ queryKey: keys.capacity });
      }
      if (changed === "blockers") qc.invalidateQueries({ queryKey: keys.backlog });
      if (changed === "final_audit")
        qc.invalidateQueries({ queryKey: keys.verification });
      if (changed === "settings")
        qc.invalidateQueries({ queryKey: keys.settings });
    });

    source.addEventListener("activity", (event) => {
      const data = JSON.parse((event as MessageEvent).data) as {
        entry?: ActivityEntry;
      };
      if (data.entry) {
        setLiveActivity((prev) => [...prev.slice(-199), data.entry!]);
        qc.invalidateQueries({ queryKey: keys.activity });
        const kind = data.entry.event;
        if (kind === "fallback" || kind === "handoff") {
          qc.invalidateQueries({ queryKey: keys.backends });
        }
      }
    });

    source.addEventListener("operation", (event) => {
      const op = JSON.parse((event as MessageEvent).data) as Operation;
      qc.invalidateQueries({ queryKey: keys.operations });
      if (op.status !== "running") {
        qc.invalidateQueries({ queryKey: keys.project });
        qc.invalidateQueries({ queryKey: keys.backlog });
        qc.invalidateQueries({ queryKey: keys.capacity });
        qc.invalidateQueries({ queryKey: keys.verification });
      }
      const previous = seenOps.current.get(op.id);
      if (previous !== op.status) {
        seenOps.current.set(op.id, op.status);
        const toast = operationToast(op);
        if (toast) pushToast(toast);
      }
    });

    return () => source.close();
  }, [qc, pushToast]);

  return (
    <LiveContext.Provider
      value={{ connection, toasts, pushToast, dismissToast, liveActivity }}
    >
      {children}
    </LiveContext.Provider>
  );
}

export function useLive() {
  return useContext(LiveContext);
}
