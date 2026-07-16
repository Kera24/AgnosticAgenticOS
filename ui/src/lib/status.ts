// Status semantics: every state maps to tone + label + icon name.
// Colour is never the only signal (design-system/MASTER.md §status).

export type Tone =
  | "ok"
  | "run"
  | "cool"
  | "warn"
  | "block"
  | "fail"
  | "idle";

export interface StatusInfo {
  tone: Tone;
  label: string;
  description?: string;
}

export function schedulerStatus(
  state: string | undefined,
  projectStatus?: string,
): StatusInfo {
  switch (state) {
    case "running":
      return { tone: "run", label: "Running" };
    case "cooling":
      return { tone: "cool", label: "Cooling" };
    case "paused":
      return { tone: "idle", label: "Paused" };
    case "complete":
      return { tone: "ok", label: "Complete" };
    case "idle":
      if (projectStatus === "blocked_on_human")
        return { tone: "block", label: "Blocked" };
      if (projectStatus === "audit_failed")
        return { tone: "fail", label: "Audit failed" };
      return { tone: "ok", label: "Ready" };
    default:
      return { tone: "idle", label: "Unknown" };
  }
}

export function taskStatus(status: string): StatusInfo {
  switch (status) {
    case "done":
      return { tone: "ok", label: "Done" };
    case "in_progress":
      return { tone: "run", label: "In progress" };
    case "blocked":
      return { tone: "block", label: "Blocked" };
    case "abandoned":
      return { tone: "idle", label: "Abandoned" };
    default:
      return { tone: "idle", label: "Pending" };
  }
}

export function breakerStatus(state: string): StatusInfo {
  switch (state) {
    case "available":
      return { tone: "ok", label: "Available" };
    case "degraded":
      return { tone: "warn", label: "Degraded" };
    case "cooling":
      return { tone: "cool", label: "Half-open" };
    case "rate_limited":
      return { tone: "warn", label: "Rate limited" };
    case "usage_exhausted":
      return { tone: "block", label: "Usage exhausted" };
    case "authentication_required":
      return { tone: "fail", label: "Auth required" };
    case "unavailable":
      return { tone: "fail", label: "Unavailable" };
    default:
      return { tone: "idle", label: state || "Unknown" };
  }
}

export function authStatus(auth: string): StatusInfo {
  switch (auth) {
    case "ok":
      return { tone: "ok", label: "Authenticated" };
    case "not-required":
      return { tone: "ok", label: "No auth needed" };
    case "required":
      return { tone: "fail", label: "Login required" };
    case "key-not-set":
      return { tone: "idle", label: "Key not set" };
    default:
      // `unknown` is NEVER treated as authenticated
      return { tone: "warn", label: "Auth unknown" };
  }
}

export function decisionStatus(decision: string): StatusInfo {
  switch (decision) {
    case "start":
      return { tone: "ok", label: "Start" };
    case "reroute":
      return { tone: "warn", label: "Reroute" };
    case "wait":
      return { tone: "cool", label: "Wait" };
    case "human_required":
      return { tone: "fail", label: "Human required" };
    default:
      return { tone: "idle", label: decision || "Unknown" };
  }
}

export function milestoneStatus(state: string): StatusInfo {
  switch (state) {
    case "done":
      return { tone: "ok", label: "Done" };
    case "in_progress":
      return { tone: "run", label: "In progress" };
    case "blocked":
      return { tone: "block", label: "Blocked" };
    default:
      return { tone: "idle", label: "Pending" };
  }
}

export function activityTone(event: string | undefined): Tone {
  if (!event) return "idle";
  if (/complete|success|started|ok|milestone/.test(event)) return "ok";
  if (/fail|error|malformed|violation|secret/.test(event)) return "fail";
  if (/blocker|human|security/.test(event)) return "block";
  if (/cooling|wait|capacity/.test(event)) return "cool";
  if (/fallback|handoff|skip|degraded|repair/.test(event)) return "warn";
  if (/cycle|review|order|invocation/.test(event)) return "run";
  return "idle";
}
