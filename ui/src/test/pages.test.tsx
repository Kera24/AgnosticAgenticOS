import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Routes, Route } from "react-router-dom";
import { mockApi, renderPage } from "./helpers";
import * as fx from "./fixtures";
import { Overview } from "../pages/Overview";
import { Build } from "../pages/Build";
import { Backends } from "../pages/Backends";
import { Capacity } from "../pages/Capacity";
import { Verification } from "../pages/Verification";
import { Settings } from "../pages/Settings";
import { Agents } from "../pages/Agents";
import { Projects } from "../pages/Projects";
import { Shell } from "../components/Shell";

const baseRoutes = {
  "/api/v1/project": fx.projectNone,
  "/api/v1/project/activity": { entries: [] },
  "/api/v1/project/backlog": { tasks: [] },
  "/api/v1/capacity": fx.capacity,
  "/api/v1/backends": { backends: fx.backends },
  "/api/v1/agents": { agents: fx.agents },
  "/api/v1/verification": fx.verification,
  "/api/v1/settings": fx.settings,
  "/api/v1/operations": { operations: [] },
};

describe("Overview", () => {
  it("shows the empty state and full orchestration rail when no project", async () => {
    mockApi(baseRoutes);
    renderPage(<Overview />);
    expect(await screen.findByText("No project started")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Orchestration pipeline" }))
      .toBeInTheDocument();
    // gate stage advertises its non-AI nature
    expect(
      screen.getByRole("button", { name: /Gate: pending.*non-AI/ }),
    ).toBeInTheDocument();
  });

  it("shows cooling status, blockers and completion meters for a live project", async () => {
    mockApi({ ...baseRoutes, "/api/v1/project": fx.projectCooling });
    renderPage(<Overview />);
    expect(await screen.findByText("needs an API key decision")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: /Tasks: 3 of 8/ }))
      .toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: /Milestones: 1 of 2/ }))
      .toBeInTheDocument();
  });
});

describe("Shell", () => {
  it("renders navigation landmarks, skip link and cooling countdown", async () => {
    mockApi({ ...baseRoutes, "/api/v1/project": fx.projectCooling });
    renderPage(
      <Routes>
        <Route element={<Shell />}>
          <Route index element={<Overview />} />
        </Route>
      </Routes>,
    );
    expect(await screen.findByText("Skip to main content")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
    expect(screen.getByRole("banner")).toBeInTheDocument();
    const countdowns = await screen.findAllByLabelText("Cooling countdown");
    expect(countdowns.length).toBeGreaterThan(0);
    expect(screen.getAllByText("Cooling").length).toBeGreaterThan(0);
  });

  it("opens the command palette with Ctrl+K and disables invalid actions with reasons", async () => {
    mockApi({ ...baseRoutes, "/api/v1/project": fx.paused });
    const user = userEvent.setup();
    renderPage(
      <Routes>
        <Route element={<Shell />}>
          <Route index element={<Overview />} />
        </Route>
      </Routes>,
    );
    await screen.findByRole("main");
    await user.keyboard("{Control>}k{/Control}");
    const palette = await screen.findByRole("dialog", { name: "Command palette" });
    expect(palette).toBeInTheDocument();
    const start = screen.getByRole("option", { name: /Start eligible cycle/ });
    expect(start).toHaveAttribute("aria-disabled", "true");
    expect(start).toHaveTextContent("paused — resume first");
    const resume = screen.getByRole("option", { name: /Resume project/ });
    expect(resume).toHaveAttribute("aria-disabled", "false");
  });
});

describe("Build Control", () => {
  it("disables start while paused and explains why; resume enabled", async () => {
    mockApi({ ...baseRoutes, "/api/v1/project": fx.paused });
    renderPage(<Build />);
    const start = await screen.findByRole("button", { name: /Start next cycle/ });
    expect(start).toBeDisabled();
    expect(start).toHaveAttribute("title", expect.stringContaining("resume"));
    expect(screen.getByRole("button", { name: /Resume/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /^Pause/ })).toBeDisabled();
  });

  it("sends pause and reflects capacity decision", async () => {
    const { calls } = mockApi({
      ...baseRoutes,
      "/api/v1/project": fx.projectCooling,
      "/api/v1/project/pause": { status: "paused" },
    });
    const user = userEvent.setup();
    renderPage(<Build />);
    expect(await screen.findByText("start", { selector: ".decision-verb" }))
      .toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^Pause/ }));
    await waitFor(() =>
      expect(
        calls.some(
          (call) =>
            call.url.includes("/project/pause") &&
            call.init?.method === "POST",
        ),
      ).toBe(true),
    );
  });
});

describe("Backends", () => {
  it("shows detection, auth (unknown never usable), breaker states", async () => {
    mockApi(baseRoutes);
    renderPage(<Backends />);
    expect(await screen.findByText("claude")).toBeInTheDocument();
    expect(screen.getByText("Auth unknown")).toBeInTheDocument();
    expect(screen.getByText("Usage exhausted")).toBeInTheDocument();
    expect(screen.getAllByText("Authenticated").length).toBe(1);
  });

  it("requires explicit confirmation before a smoke test", async () => {
    const { calls } = mockApi(baseRoutes);
    const user = userEvent.setup();
    renderPage(<Backends />);
    const buttons = await screen.findAllByRole("button", { name: /Smoke test/ });
    await user.click(buttons[0]);
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/consumes real subscription allowance/i);
    // no request until confirmed
    expect(calls.some((call) => call.url.includes("smoke-test"))).toBe(false);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("Capacity", () => {
  it("labels estimates and never presents them as quotas", async () => {
    mockApi(baseRoutes);
    renderPage(<Capacity />);
    expect(
      await screen.findByText(/estimated from local history/),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/estimate/i).length).toBeGreaterThan(1);
    expect(screen.getByText("×1.35")).toBeInTheDocument();
  });

  it("offers an accessible table alternative to the usage chart", async () => {
    mockApi(baseRoutes);
    const user = userEvent.setup();
    renderPage(<Capacity />);
    await screen.findByText(/Usage by backend/);
    await user.click(screen.getByRole("button", { name: "table view" }));
    expect(screen.getByRole("columnheader", { name: "Calls 24h" }))
      .toBeInTheDocument();
  });
});

describe("Verification", () => {
  it("shows configured checks, baseline and verdicts", async () => {
    mockApi(baseRoutes);
    renderPage(<Verification />);
    expect(await screen.findByText("python -m pytest -q")).toBeInTheDocument();
    expect(screen.getAllByText("Passed").length).toBeGreaterThan(0);
    expect(screen.getByText("Pass")).toBeInTheDocument(); // QA verdict
    expect(screen.getByText(/security-relevant/)).toBeInTheDocument();
  });

  it("presents zero configured checks as blocking", async () => {
    mockApi({
      ...baseRoutes,
      "/api/v1/verification": {
        ...fx.verification,
        configured: false,
        commands: [],
        latest: { run: null, results: [] },
      },
    });
    renderPage(<Verification />);
    expect(
      await screen.findByText(/blocks all cycles/i),
    ).toBeInTheDocument();
  });
});

describe("Agents", () => {
  it("marks read-only vs write roles and the non-AI gate", async () => {
    mockApi(baseRoutes);
    renderPage(<Agents />);
    expect(await screen.findByText("Project Architect")).toBeInTheDocument();
    expect(screen.getAllByText("read-only").length).toBeGreaterThan(0);
    expect(screen.getAllByText("workspace-write").length).toBeGreaterThan(0);
    expect(screen.getAllByText("non-AI").length).toBeGreaterThan(0);
  });
});

describe("Projects", () => {
  it("previews a pasted plan before starting and validates length", async () => {
    const plan = "# App\n\n" + "Build a thing with tests. ".repeat(5);
    mockApi({
      ...baseRoutes,
      "/api/v1/project/plan/preview": {
        source: "(pasted plan)",
        length: plan.length,
        content: plan,
      },
    });
    const user = userEvent.setup();
    renderPage(<Projects />);
    const textarea = await screen.findByLabelText(/Application plan/);
    await user.click(screen.getByRole("button", { name: "Preview plan" }));
    // too short/empty: button disabled
    expect(screen.getByRole("button", { name: "Preview plan" })).toBeDisabled();
    await user.type(textarea, plan.slice(0, 120));
    expect(screen.getByRole("button", { name: "Preview plan" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Preview plan" }));
    expect(await screen.findByText(/preview · \(pasted plan\)/)).toBeInTheDocument();
  });
});

describe("Settings", () => {
  it("renders current values and saves via PUT", async () => {
    const { calls } = mockApi({
      ...baseRoutes,
      "/api/v1/settings": (init?: RequestInit) => {
        if (init?.method === "PUT") {
          return new Response(
            JSON.stringify({ saved: true, settings: fx.settings }),
            { status: 200 },
          );
        }
        return new Response(JSON.stringify(fx.settings), { status: 200 });
      },
    });
    const user = userEvent.setup();
    renderPage(<Settings />);
    const port = await screen.findByLabelText("Default port");
    expect(port).toHaveValue("8765");
    await user.clear(port);
    await user.type(port, "9100");
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => {
      const put = calls.find((call) => call.init?.method === "PUT");
      expect(put).toBeTruthy();
      const body = JSON.parse(String(put!.init!.body));
      expect(body.ui.port).toBe(9100);
      // nothing credential-shaped ever leaves the form
      expect(JSON.stringify(body)).not.toMatch(
        /api[_-]?key|auth[_-]?token|secret|password|credential/i,
      );
    });
  });

  it("shows a validation error from the server inline", async () => {
    mockApi({
      ...baseRoutes,
      "/api/v1/settings": (init?: RequestInit) => {
        if (init?.method === "PUT") {
          return new Response(
            JSON.stringify({ detail: "ui.port must be between 1024 and 65535" }),
            { status: 422 },
          );
        }
        return new Response(JSON.stringify(fx.settings), { status: 200 });
      },
    });
    const user = userEvent.setup();
    renderPage(<Settings />);
    await screen.findByLabelText("Default port");
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "ui.port must be between 1024 and 65535",
    );
  });
});
