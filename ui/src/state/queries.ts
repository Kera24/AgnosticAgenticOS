import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api } from "../lib/api";
import type {
  ActivityEntry,
  Agent,
  Backend,
  CapacitySnapshot,
  DoctorCheck,
  Operation,
  PlanDocuments,
  ProjectSnapshot,
  Settings,
  Task,
  VerificationSnapshot,
} from "../lib/types";

export const keys = {
  project: ["project"] as const,
  backlog: ["project", "backlog"] as const,
  plan: ["project", "plan"] as const,
  activity: ["activity"] as const,
  agents: ["agents"] as const,
  backends: ["backends"] as const,
  capacity: ["capacity"] as const,
  verification: ["verification"] as const,
  settings: ["settings"] as const,
  operations: ["operations"] as const,
  doctor: ["doctor"] as const,
};

export function useProject() {
  return useQuery({
    queryKey: keys.project,
    queryFn: () => api.get<ProjectSnapshot>("/project"),
    refetchInterval: 30_000,
  });
}

export function useBacklog() {
  return useQuery({
    queryKey: keys.backlog,
    queryFn: () => api.get<{ tasks: Task[] }>("/project/backlog"),
  });
}

export function usePlan(enabled = true) {
  return useQuery({
    queryKey: keys.plan,
    queryFn: () => api.get<PlanDocuments>("/project/plan"),
    enabled,
  });
}

export function useActivity(limit = 300) {
  return useQuery({
    queryKey: [...keys.activity, limit],
    queryFn: () =>
      api.get<{ entries: ActivityEntry[] }>(
        `/project/activity?limit=${limit}`,
      ),
  });
}

export function useAgents() {
  return useQuery({
    queryKey: keys.agents,
    queryFn: () => api.get<{ agents: Agent[] }>("/agents"),
  });
}

export function useBackends() {
  return useQuery({
    queryKey: keys.backends,
    queryFn: () => api.get<{ backends: Backend[] }>("/backends"),
    staleTime: 60_000,
  });
}

export function useCapacity() {
  return useQuery({
    queryKey: keys.capacity,
    queryFn: () => api.get<CapacitySnapshot>("/capacity"),
  });
}

export function useVerification() {
  return useQuery({
    queryKey: keys.verification,
    queryFn: () => api.get<VerificationSnapshot>("/verification"),
  });
}

export function useSettings() {
  return useQuery({
    queryKey: keys.settings,
    queryFn: () => api.get<Settings>("/settings"),
  });
}

export function useOperations() {
  return useQuery({
    queryKey: keys.operations,
    queryFn: () => api.get<{ operations: Operation[] }>("/operations"),
  });
}

export function useDoctor(enabled: boolean) {
  return useQuery({
    queryKey: keys.doctor,
    queryFn: () =>
      api.get<{ ok: boolean; checks: DoctorCheck[] }>("/doctor"),
    enabled,
    staleTime: 120_000,
  });
}

export function useRunLog(run: string | null, name: string | null) {
  return useQuery({
    queryKey: ["log", run, name],
    queryFn: () =>
      api.get<{ run: string; name: string; content: string }>(
        `/logs/${encodeURIComponent(run!)}/${encodeURIComponent(name!)}`,
      ),
    enabled: Boolean(run && name),
  });
}

// -- mutations -----------------------------------------------------------------

export function useProjectAction(
  path: "run" | "resume" | "pause" | "review",
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<Record<string, unknown>>(`/project/${path}`),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: keys.project });
      qc.invalidateQueries({ queryKey: keys.operations });
    },
  });
}

export function useStartProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { plan_text?: string; plan_path?: string }) =>
      api.post<Operation>("/project/start", body),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: keys.project });
      qc.invalidateQueries({ queryKey: keys.operations });
    },
  });
}

export function usePlanPreview() {
  return useMutation({
    mutationFn: (body: { plan_text?: string; plan_path?: string }) =>
      api.post<{ source: string; length: number; content: string }>(
        "/project/plan/preview",
        body,
      ),
  });
}

export function useBackendsRefresh() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<{ backends: Backend[] }>("/backends/refresh"),
    onSuccess: (data) => qc.setQueryData(keys.backends, data),
  });
}

export function useSmokeTest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) =>
      api.post<Operation>(
        `/backends/${encodeURIComponent(name)}/smoke-test`,
        { confirm: true },
      ),
    onSettled: () => qc.invalidateQueries({ queryKey: keys.operations }),
  });
}

export function useResetBreaker() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) =>
      api.post<{ backend: string; state: string }>(
        `/backends/${encodeURIComponent(name)}/reset-breaker`,
        { confirm: true },
      ),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: keys.backends });
      qc.invalidateQueries({ queryKey: keys.capacity });
    },
  });
}

export function useSaveSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api.put<{ saved: boolean; settings: Settings }>("/settings", body),
    onSuccess: (data) => qc.setQueryData(keys.settings, data.settings),
  });
}
