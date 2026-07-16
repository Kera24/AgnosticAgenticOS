// Typed contracts for /api/v1 — mirrors .agentic/ui responses.

export interface SchedulerState {
  state: "idle" | "running" | "cooling" | "paused" | "complete" | string;
  current_cycle: string | null;
  next_run_at: string | null;
  cooling_reason: string | null;
  selected_backend: string | null;
  project_status: string;
  last_heartbeat: string | null;
}

export interface Task {
  id: string;
  milestone: string | null;
  description: string;
  dependencies: string[];
  risk: string;
  security_relevant: boolean;
  expected_size: string;
  status: "pending" | "in_progress" | "done" | "blocked" | "abandoned";
  attempts: number;
  last_result: string | null;
  blocking_reason: string | null;
  acceptance_criteria: string[];
}

export interface Blocker {
  task: string | null;
  reason: string;
  human_only: boolean;
  created_at: string;
  resolved: boolean;
}

export interface Milestone {
  id: string;
  title?: string;
  description?: string;
}

export interface Progress {
  updated_at?: string;
  tasks_total?: number;
  tasks_by_status?: Record<string, number>;
  milestones?: Record<string, string>;
  backlog_complete?: boolean;
}

export interface FinalAudit {
  completed_at: string;
  complete: boolean;
  checks: Record<string, boolean>;
  final_review: { verdict?: string; reason?: string } | null;
  completion_criteria: string[];
  branch: string;
}

export interface ProjectSnapshot {
  exists: boolean;
  name: string | null;
  scheduler: SchedulerState;
  eligible: boolean | null;
  eligible_reason: string | null;
  progress: Progress;
  blockers: Blocker[];
  human_blockers: Blocker[];
  milestones: Milestone[];
  backlog_summary: Record<string, number>;
  next_task: Task | null;
  branch: string;
  worktree: string;
  worktree_exists: boolean;
  repository_root: string;
  final_audit: FinalAudit | null;
  human_decisions: string[];
}

export interface Agent {
  id: string;
  name: string;
  ai: boolean;
  purpose: string;
  can_edit: boolean;
  permissions: string;
  conditional: boolean;
  chain: string[];
  last_invocation: string | null;
  last_backend: string | null;
  last_ok: boolean | null;
  recent_calls: number;
  recent_failures: number;
  recent_avg_duration_seconds: number | null;
  recent_tokens: number;
  tokens_estimated: boolean;
}

export interface Backend {
  name: string;
  type: string;
  classification: "cli" | "local" | "api";
  detected: boolean;
  version: string | null;
  auth: string;
  models: string[];
  model: string | null;
  smoke_test_passed: boolean | null;
  breaker_state: string;
  unavailable_until: string | null;
  last_ok: string | null;
  last_failure_kind: string | null;
  consecutive_failures: number;
  roles: string[];
  is_primary: boolean;
  in_fallbacks: boolean;
  usable: boolean | null;
  api_key_env?: string;
}

export interface CapacityEstimate {
  estimated_cycle_tokens: number;
  highest_recent_cycle_tokens: number;
  required_capacity_tokens: number;
  safety_multiplier: number;
  history_samples: number;
}

export interface CapacityDecision {
  decision: "start" | "reroute" | "wait" | "human_required";
  confidence: "reported" | "estimated" | "unknown";
  selected_backend: string | null;
  required_estimated_tokens: number;
  available_estimated_tokens: number | null;
  safety_reserve_tokens: number;
  fallback_candidates: string[];
  wait_until: string | null;
  reason: string;
}

export interface BackendCapacity {
  name: string;
  calls_last_hour: number;
  calls_last_day: number;
  tokens_last_day: {
    input: number;
    cached: number;
    output: number;
    reasoning: number;
  };
  tokens_estimated: boolean;
  limit_reasons: string[];
  remaining_under_limits: number | null;
  limits: Record<string, number | null>;
  breaker_state: string;
  unavailable_until: string | null;
}

export interface CycleRow {
  timestamp: string;
  run_id: string;
  backend: string;
  skill: string;
  task_size: string;
  total_tokens: string;
  duration_seconds: string;
  result: string;
}

export interface CapacitySnapshot {
  note: string;
  next_task: string | null;
  chain: string[];
  estimate: CapacityEstimate | null;
  decision: CapacityDecision | null;
  safety_multiplier: number;
  per_backend: BackendCapacity[];
  recent_cycles: CycleRow[];
  moving_average_cycle_tokens: number | null;
  limit_events: ActivityEntry[];
}

export interface CheckResult {
  name: string;
  run?: string;
  log_file?: string;
  exit_code: number | null;
  passed: boolean;
  excerpt: string;
  known_baseline_failure?: boolean;
  new_regression?: boolean;
}

export interface VerificationSnapshot {
  configured: boolean;
  auto_detected: boolean;
  commands: { name: string; command: string; mandatory: boolean }[];
  no_checks_is_blocking: boolean;
  baseline: { recorded_at: string; checks: Record<string, boolean> } | null;
  latest: { run: string | null; attempt_dir?: string | null; results: CheckResult[] };
  qa: ActivityEntry | null;
  security: ActivityEntry | null;
  final_audit: FinalAudit | null;
}

export interface ActivityEntry {
  ts?: string;
  event?: string;
  source?: string;
  [key: string]: unknown;
}

export interface Operation {
  id: string;
  kind: string;
  group: string;
  status: "running" | "succeeded" | "failed" | "interrupted";
  detail: string;
  started_at: string;
  finished_at: string | null;
  result: Record<string, unknown> | null;
  error: string | null;
}

export interface Settings {
  interaction: { mode: string };
  routing: {
    mode: "simple" | "per_agent";
    primary: string | null;
    fallbacks: string[];
    per_agent: Record<string, { primary: string; fallbacks: string[] }>;
  };
  cycle: {
    target_duration_minutes: number;
    maximum_duration_minutes: number;
  };
  cooling: {
    after_success_minutes: number;
    after_failure_minutes: number;
    minimum_minutes: number;
    maximum_minutes: number;
  };
  capacity: { safety_multiplier: number };
  limits: Record<string, Record<string, number | null>>;
  operating_window: {
    enabled: boolean;
    start: string;
    stop: string;
    timezone: string;
  };
  notifications: { desktop: boolean };
  ui: {
    port: number;
    open_browser: boolean;
    theme: "dark" | "light";
    reduced_motion: boolean | "system";
  };
  backends_configured: string[];
}

export interface DoctorCheck {
  level: "ok" | "warn" | "error";
  message: string;
}

export interface PlanDocuments {
  plan: string | null;
  architecture: string | null;
  acceptance_criteria: {
    requirements_map?: { requirement: string; tasks?: string[] }[];
    completion_criteria?: string[];
  };
}
