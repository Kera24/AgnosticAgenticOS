import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HashRouter, Route, Routes } from "react-router-dom";
import { LiveProvider } from "./state/events";
import { Shell } from "./components/Shell";
import { Overview } from "./pages/Overview";
import { Projects } from "./pages/Projects";
import { Build } from "./pages/Build";
import { Agents } from "./pages/Agents";
import { Backends } from "./pages/Backends";
import { Capacity } from "./pages/Capacity";
import { Verification } from "./pages/Verification";
import { Activity } from "./pages/Activity";
import { Settings } from "./pages/Settings";
import { Context } from "./pages/Context";
import { Portfolio } from "./pages/Portfolio";
import { Mcp } from "./pages/Mcp";
import { Memory } from "./pages/Memory";
import { Knowledge } from "./pages/Knowledge";
import { Skills } from "./pages/Skills";
import { Routing } from "./pages/Routing";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      retry: 1,
      refetchOnWindowFocus: true,
    },
  },
});

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <LiveProvider>
        <HashRouter>
          <Routes>
            <Route element={<Shell />}>
              <Route index element={<Overview />} />
              <Route path="portfolio" element={<Portfolio />} />
              <Route path="mcp" element={<Mcp />} />
              <Route path="projects" element={<Projects />} />
              <Route path="build" element={<Build />} />
              <Route path="agents" element={<Agents />} />
              <Route path="backends" element={<Backends />} />
              <Route path="routing" element={<Routing />} />
              <Route path="context" element={<Context />} />
              <Route path="memory" element={<Memory />} />
              <Route path="knowledge" element={<Knowledge />} />
              <Route path="skills" element={<Skills />} />
              <Route path="capacity" element={<Capacity />} />
              <Route path="verification" element={<Verification />} />
              <Route path="activity" element={<Activity />} />
              <Route path="settings" element={<Settings />} />
              <Route path="*" element={<Overview />} />
            </Route>
          </Routes>
        </HashRouter>
      </LiveProvider>
    </QueryClientProvider>
  );
}
