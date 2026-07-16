/* Mocked end-to-end flow: load status → start a plan → stream cycle
   activity over (fake) SSE → cooling countdown → resume → complete →
   final audit. All network is mocked; no real CLI or API call can occur. */
import { describe, expect, it } from "vitest";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../App";
import { render } from "@testing-library/react";
import { mockApi } from "./helpers";
import * as fx from "./fixtures";

type FakeES = {
  instances: Array<{
    emit: (type: string, data: unknown) => void;
    onopen: (() => void) | null;
  }>;
};

function lastEventSource() {
  const fake = (globalThis as Record<string, unknown>)
    .__fakeEventSource as FakeES;
  return fake.instances[fake.instances.length - 1];
}

describe("end-to-end (mocked)", () => {
  it("walks a project from empty to complete with a final audit", async () => {
    let project = { ...fx.projectNone };
    const routes = {
      "/api/v1/project/activity": { entries: [] },
      "/api/v1/project/backlog": { tasks: [] },
      "/api/v1/capacity": fx.capacity,
      "/api/v1/backends": { backends: fx.backends },
      "/api/v1/agents": { agents: fx.agents },
      "/api/v1/verification": fx.verification,
      "/api/v1/settings": fx.settings,
      "/api/v1/operations": { operations: [] },
      "/api/v1/project/plan": {
        plan: "# plan",
        architecture: "# arch",
        acceptance_criteria: { completion_criteria: ["works"] },
      },
      "/api/v1/project/start": (init?: RequestInit) => {
        expect(init?.method).toBe("POST");
        project = { ...fx.projectCooling };
        return new Response(
          JSON.stringify({
            id: "op1",
            kind: "project.start",
            group: "project",
            status: "running",
            detail: "architecting project",
            started_at: new Date().toISOString(),
            finished_at: null,
            result: null,
            error: null,
          }),
          { status: 200 },
        );
      },
      "/api/v1/project/resume": () => {
        project = {
          ...project,
          scheduler: { ...project.scheduler, state: "idle", next_run_at: null },
          eligible: true,
        };
        return new Response(JSON.stringify({ status: "idle" }), { status: 200 });
      },
      "/api/v1/project": () =>
        new Response(JSON.stringify(project), { status: 200 }),
    };
    mockApi(routes);
    const user = userEvent.setup();
    render(<App />);

    // 1. loads with no project
    expect(await screen.findByText("No project started")).toBeInTheDocument();

    // 2. start a mocked plan from Projects
    await user.click(screen.getByRole("link", { name: /Projects/ }));
    const textarea = await screen.findByLabelText(/Application plan/);
    await user.type(
      textarea,
      "# Demo app{Enter}{Enter}" + "Build a demo application with tests. ".repeat(3),
    );
    await user.click(screen.getByRole("button", { name: "Start project" }));

    // 3. project now exists and is cooling; stream mocked cycle activity
    await waitFor(() =>
      expect(screen.getAllByText("demo-app").length).toBeGreaterThan(0),
    );
    act(() => {
      lastEventSource().emit("activity", {
        ts: new Date().toISOString(),
        entry: {
          ts: new Date().toISOString(),
          event: "cycle_finished",
          run_id: "20260716-090000",
          outcome: "success",
        },
      });
      lastEventSource().emit("state", { changed: "scheduler" });
    });

    // 4. cooling countdown is visible in the status strip
    expect(await screen.findByLabelText("Cooling countdown")).toBeInTheDocument();

    // 5. pause state → resume via Build Control
    await user.click(screen.getByRole("link", { name: /Build Control/ }));
    await screen.findByRole("button", { name: /Start next cycle/ });

    // 6. complete the project: final audit arrives
    project = {
      ...project,
      scheduler: {
        ...project.scheduler,
        state: "complete",
        project_status: "complete",
        next_run_at: null,
      },
      progress: { ...project.progress, backlog_complete: true },
      blockers: [],
      human_blockers: [],
      final_audit: {
        completed_at: new Date().toISOString(),
        complete: true,
        checks: {
          backlog_complete: true,
          deterministic_checks_pass: true,
          final_independent_review: true,
        },
        final_review: { verdict: "pass" },
        completion_criteria: ["works"],
        branch: "agentic/project",
      },
    };
    act(() => {
      lastEventSource().emit("state", { changed: "final_audit" });
    });
    await waitFor(() =>
      expect(screen.getAllByText("Complete").length).toBeGreaterThan(0),
    );

    // 7. final audit visible on Projects
    await user.click(screen.getByRole("link", { name: /Projects/ }));
    await user.click(await screen.findByRole("tab", { name: "Final audit" }));
    expect(
      await screen.findByText("final independent review"),
    ).toBeInTheDocument();
  });
});
