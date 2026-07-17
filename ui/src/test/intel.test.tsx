/* Phase 10 pages: context, memory, knowledge, skills, routing —
   contracts, confirmation gates, empty/degraded states, honest labels. */
import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockApi, renderPage } from "./helpers";
import { Context } from "../pages/Context";
import { Memory } from "../pages/Memory";
import { Knowledge } from "../pages/Knowledge";
import { Skills } from "../pages/Skills";
import { Routing } from "../pages/Routing";

const contextView = {
  code_intelligence: {
    provider: "native",
    configured_provider: "cce",
    indexed: true,
    stale: false,
    revision: "abc123def456",
    files_indexed: 42,
    indexed_at: "2026-07-18T10:00:00",
    health: { ok: true, detail: null },
    fallback_reason: "cce unavailable: binary not found",
  },
  packages: [
    {
      package_id: "pkg1",
      role: "coder",
      created_at: "2026-07-18T10:05:00",
      token_budget: 64000,
      token_estimate: 12000,
      reserved_output_tokens: 12000,
      tokens_by_category: { policy: 800, code: 9000 },
      estimated_savings_tokens: 44000,
      candidate_total_tokens: 56000,
      included: [
        {
          id: "i1",
          category: "code",
          source_path: "src/app.py",
          token_estimate: 9000,
          trust_level: "untrusted",
          reason: "selected (relevance 0.65)",
        },
      ],
      omitted: [
        {
          item: {
            id: "i2",
            category: "code",
            source_path: "src/big.py",
            token_estimate: 30000,
            trust_level: "untrusted",
          },
          reason: "allocation cap for 'code' reached",
        },
      ],
    },
  ],
  totals: {
    estimated_savings_tokens: 44000,
    token_estimate: 12000,
    measurement: "estimated",
  },
};

describe("Context", () => {
  it("shows index status, fallback reason and package composition", async () => {
    mockApi({ "/api/v1/context": contextView });
    renderPage(<Context />);
    expect(await screen.findByText("native healthy")).toBeInTheDocument();
    expect(
      screen.getByText("cce unavailable: binary not found"),
    ).toBeInTheDocument();
    // savings are labelled as estimates, never provider figures
    expect(screen.getByText(/tokens saved vs candidates \(est\)/))
      .toBeInTheDocument();
    // open the package drawer
    await userEvent.setup().click(screen.getByText("coder"));
    expect(await screen.findByText(/allocation cap for 'code' reached/))
      .toBeInTheDocument();
    expect(screen.getByText(/untrusted/)).toBeInTheDocument();
  });

  it("renders an empty state without packages", async () => {
    mockApi({
      "/api/v1/context": {
        ...contextView,
        packages: [],
        totals: { estimated_savings_tokens: 0, token_estimate: 0 },
      },
    });
    renderPage(<Context />);
    expect(
      await screen.findByText(/no packages yet/),
    ).toBeInTheDocument();
  });
});

const memoryView = {
  project_id: "demo",
  total_records: 2,
  by_type: {},
  records: [
    {
      id: "mem1",
      type: "implementation_decision",
      title: "use sqlite",
      compact_summary: "sqlite for persistence",
      importance: 0.6,
      reviewer_verified: 1,
      status: "active",
      created_at: "2026-07-18T09:00:00",
      task_id: null,
    },
  ],
};

describe("Memory", () => {
  it("lists compact records and requires confirmation to forget", async () => {
    const { calls } = mockApi({
      "/api/v1/memory": memoryView,
      "/api/v1/memory/records": {
        records: [{ ...memoryView.records[0], details: "full detail",
                    source: "cycle", supersedes: null, tags: [],
                    related_paths: [], sensitive: 0,
                    updated_at: "2026-07-18T09:00:00" }],
      },
      "/api/v1/memory/mem1/timeline": { timeline: memoryView.records },
      "/api/v1/memory/forget": { forgotten: 1 },
    });
    renderPage(<Memory />);
    const user = userEvent.setup();
    await user.click(await screen.findByText("use sqlite"));
    expect(await screen.findByText("full detail")).toBeInTheDocument();
    await user.click(screen.getByText("Forget this record…"));
    // dialog appears; nothing sent yet
    expect(
      calls.filter((c) => c.url.includes("/memory/forget")),
    ).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Forget" }));
    await waitFor(() =>
      expect(
        calls.filter((c) => c.url.includes("/memory/forget")),
      ).toHaveLength(1),
    );
    const body = JSON.parse(
      String(calls.find((c) => c.url.includes("/memory/forget"))!.init!.body),
    );
    expect(body).toEqual({ id: "mem1", confirm: true });
  });
});

describe("Knowledge", () => {
  it("shows conflicts and validation issues", async () => {
    mockApi({
      "/api/v1/knowledge": {
        root: "C:/repo/.agentic/knowledge",
        documents: 2,
        conflicts: ["current-state.incoming.md"],
        issues: ["current-state.md: generated section edited by user"],
        docs: [
          { path: "project-overview.md", id: "project-overview",
            type: "overview", updated: "2026-07-18T08:00:00",
            conflict: false },
          { path: "current-state.incoming.md", id: null, type: null,
            updated: null, conflict: true },
        ],
      },
    });
    renderPage(<Knowledge />);
    expect(await screen.findByText(/current-state\.incoming\.md — your edit/))
      .toBeInTheDocument();
    expect(
      screen.getByText(/generated section edited by user/),
    ).toBeInTheDocument();
    expect(screen.getByText("conflict")).toBeInTheDocument();
  });
});

const skills = [
  {
    id: "testing",
    name: "Testing",
    description: "testing guidance",
    source: "builtin",
    pinned_revision: "builtin",
    checksum: "aabbccddeeff00112233",
    license: "repository",
    compatible_agents: ["coder", "qa"],
    triggers: ["test"],
    scripts: [],
    permissions: ["read"],
    reviewed: true,
    reviewed_at: "2026-07-18T08:00:00",
    enabled: true,
    risk_level: "low",
  },
  {
    id: "scripty",
    name: "Scripty",
    description: "has scripts",
    source: "/tmp/scripty",
    pinned_revision: "abc1234",
    checksum: "deadbeef001122334455",
    license: "MIT",
    compatible_agents: ["coder"],
    triggers: ["widget"],
    scripts: ["setup.sh"],
    permissions: ["read"],
    reviewed: false,
    reviewed_at: null,
    enabled: false,
    risk_level: "high",
    scan_findings: ["setup.sh: suspicious token 'curl '"],
  },
];

describe("Skills", () => {
  it("lists skills with risk state and confirms disable", async () => {
    const { calls } = mockApi({
      "/api/v1/skills": { skills },
      "/api/v1/skills/testing/disable": { ...skills[0], enabled: false },
    });
    renderPage(<Skills />);
    const user = userEvent.setup();
    expect(await screen.findByText("testing")).toBeInTheDocument();
    expect(screen.getByText("high")).toBeInTheDocument();
    await user.click(screen.getAllByText("disable…")[0]);
    await user.click(screen.getByRole("button", { name: "Disable" }));
    await waitFor(() =>
      expect(
        calls.filter((c) => c.url.includes("/skills/testing/disable")),
      ).toHaveLength(1),
    );
    const body = JSON.parse(
      String(
        calls.find((c) => c.url.includes("/skills/testing/disable"))!.init!
          .body,
      ),
    );
    expect(body).toEqual({ confirm: true });
  });

  it("surfaces scan findings in the inspector", async () => {
    mockApi({ "/api/v1/skills": { skills } });
    renderPage(<Skills />);
    await userEvent.setup().click(await screen.findByText("scripty"));
    expect(await screen.findByText(/suspicious token/)).toBeInTheDocument();
  });
});

describe("Routing", () => {
  it("shows policies and decision explanations", async () => {
    mockApi({
      "/api/v1/routing": {
        mode: "capability",
        primary: null,
        fallbacks: [],
        per_agent: {},
        agents: { coder: { capabilities: { coding: "high" } } },
        policies: {
          reviewer_different_from_worker: true,
          no_fallback_on_auth_failure: true,
          no_fallback_on_refusal: true,
          allow_local_fallback: true,
        },
        decisions: [
          {
            at: "2026-07-18T10:00:00",
            role: "coder",
            mode: "capability",
            chain: ["codex", "claude"],
            required_capabilities: { coding: "high" },
            rejected: [
              { backend: "ollama",
                reason: "embedding model 'nomic-embed' cannot serve "
                        + "generative roles" },
            ],
            candidates: [
              { backend: "codex", strength: 10, breaker_state: "available",
                success_rate: 0.9, preferred_rank: 0 },
            ],
            warnings: [],
          },
        ],
      },
    });
    renderPage(<Routing />);
    expect(await screen.findByText("reviewer different from worker"))
      .toBeInTheDocument();
    await userEvent
      .setup()
      .click(within(screen.getByRole("table")).getByText("coder"));
    expect(await screen.findByText(/embedding model/)).toBeInTheDocument();
  });
});
