/* MP Phase 9 pages: portfolio waiting reasons + confirmed destructive
   actions; MCP review/enable flow. */
import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockApi, renderPage } from "./helpers";
import { Portfolio } from "../pages/Portfolio";
import { Mcp } from "../pages/Mcp";

const project = {
  id: "restaurant-ordering",
  name: "Restaurant Ordering",
  root_path: "C:\\Agentic\\projects\\restaurant-ordering",
  plan_path: "plan.md",
  default_branch: "main",
  agentic_branch: "agentic/project",
  status: "initialised",
  enabled: true,
  priority: 50,
  state: "queued",
  waiting_reason: "eligible",
  scheduler: { state: "idle", next_run_at: null,
               selected_backend: "claude", current_cycle: null },
  progress: { done: 3, pending: 5 },
  worktree: "C:\\Users\\me\\.agentic-os\\projects\\restaurant-ordering\\worktrees\\project",
  task_worktrees: ["t4-menu"],
  lease: null,
  docker: { detected: true, compose_project: "agentic-restaurant-ordering" },
  supabase: { detected: true, project_ref: "abcd1234" },
  code_index: { provider: "native", files_indexed: 120 },
  runtime_dir: "C:\\Users\\me\\.agentic-os\\projects\\restaurant-ordering",
};

const portfolioView = {
  projects: [project],
  runtime_home: "C:\\Users\\me\\.agentic-os",
  authorised_roots: [],
};

const fleetView = {
  global_pause: false,
  limits: { maximum_active_projects: 4 },
  slots: { active_projects: 1, model: 1, per_backend: { claude: 1 } },
  would_start: [{ project: "restaurant-ordering", backend: "claude" }],
  waiting: [{ project: "restaurant-ordering",
              reason: "waiting for a claude slot" }],
};

describe("Portfolio", () => {
  it("shows absolute paths, states and waiting reasons", async () => {
    mockApi({ "/api/v1/portfolio": portfolioView,
              "/api/v1/fleet": fleetView });
    renderPage(<Portfolio />);
    expect(await screen.findByText("restaurant-ordering"))
      .toBeInTheDocument();
    expect(screen.getByText(/waiting for a claude slot/))
      .toBeInTheDocument();
    expect(
      screen.getByText("C:\\Agentic\\projects\\restaurant-ordering"),
    ).toBeInTheDocument();
    expect(screen.getByText(/1\/4/)).toBeInTheDocument();
  });

  it("requires confirmation for archive and never hides the target",
     async () => {
    const { calls } = mockApi({
      "/api/v1/portfolio": portfolioView,
      "/api/v1/fleet": fleetView,
      "/api/v1/portfolio/restaurant-ordering/archive":
        { status: "archived" },
    });
    renderPage(<Portfolio />);
    const user = userEvent.setup();
    await user.click(await screen.findByText("archive…"));
    // dialog names the exact project; nothing sent yet
    expect(
      calls.filter((c) => c.url.includes("/archive")),
    ).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "archive" }));
    await waitFor(() =>
      expect(calls.filter((c) => c.url.includes("/archive")))
        .toHaveLength(1));
    const body = JSON.parse(String(
      calls.find((c) => c.url.includes("/archive"))!.init!.body));
    expect(body).toEqual({ confirm: true });
  });

  it("drawer shows worktrees, lease, docker and supabase detail",
     async () => {
    mockApi({ "/api/v1/portfolio": portfolioView,
              "/api/v1/fleet": fleetView });
    renderPage(<Portfolio />);
    await userEvent.setup()
      .click(await screen.findByText("restaurant-ordering"));
    expect(await screen.findByText(/agentic-restaurant-ordering/))
      .toBeInTheDocument();
    expect(screen.getByText(/t4-menu/)).toBeInTheDocument();
    expect(screen.getByText(/ref abcd1234/)).toBeInTheDocument();
    expect(screen.getByText(/native · 120 files/)).toBeInTheDocument();
  });
});

const server = {
  id: "supabase-local", name: "Supabase local", transport: "stdio",
  command: "npx", args: ["-y", "@supabase/mcp"], url: null,
  scope: "project", project_id: "restaurant-ordering",
  environment: "local", read_only: true,
  authentication_type: "none", authentication_status: "unconfigured",
  allowed_tools: ["list_tables"], denied_tools: [],
  maximum_output_tokens: 4000, timeout: 30,
  enabled: false, reviewed: false, risk_level: "medium",
  last_health_check: null,
};

describe("Mcp", () => {
  it("shows unreviewed state and enables only after review + confirm",
     async () => {
    const { calls } = mockApi({
      "/api/v1/mcp": { servers: [server] },
      "/api/v1/mcp/supabase-local/review": { ...server, reviewed: true },
      "/api/v1/mcp/supabase-local/enable":
        { ...server, reviewed: true, enabled: true },
    });
    renderPage(<Mcp />);
    const user = userEvent.setup();
    expect(await screen.findByText("unreviewed")).toBeInTheDocument();
    await user.click(screen.getByText("mark reviewed"));
    await user.click(screen.getByText("enable…"));
    await user.click(screen.getByRole("button", { name: "enable" }));
    await waitFor(() =>
      expect(calls.filter((c) => c.url.includes("/enable")))
        .toHaveLength(1));
    const body = JSON.parse(String(
      calls.find((c) => c.url.includes("/enable"))!.init!.body));
    expect(body).toEqual({ confirm: true });
  });
});
