import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { render } from "@testing-library/react";
import { vi } from "vitest";
import type { ReactNode } from "react";
import { LiveProvider } from "../state/events";

/** Route-aware fetch mock: map "/api/v1/..." paths to JSON payloads. */
export function mockApi(routes: Record<string, unknown>) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    const path = url.replace(/^https?:\/\/[^/]+/, "");
    for (const [route, payload] of Object.entries(routes)) {
      if (path === route || path.startsWith(route + "?")) {
        if (typeof payload === "function") {
          return (payload as (init?: RequestInit) => Response)(init);
        }
        return new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
    }
    return new Response(JSON.stringify({ detail: "not found" }), {
      status: 404,
    });
  });
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  return { fetchMock, calls };
}

export function renderPage(ui: ReactNode, { route = "/" } = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  return render(
    <QueryClientProvider client={client}>
      <LiveProvider>
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </LiveProvider>
    </QueryClientProvider>,
  );
}
