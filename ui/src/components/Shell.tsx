import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import {
  Activity,
  Bot,
  BookOpen,
  BrainCircuit,
  Database,
  FolderGit2,
  Gauge,
  LayoutDashboard,
  Play,
  Puzzle,
  Route as RouteIcon,
  Server,
  Settings as SettingsIcon,
  ShieldCheck,
  Waypoints,
  WifiOff,
} from "lucide-react";
import { useProject, useSettings } from "../state/queries";
import { schedulerStatus } from "../lib/status";
import { useLive } from "../state/events";
import { BackendChip, Countdown, StatusChip, Toasts } from "./ui";
import { CommandPalette } from "./CommandPalette";
import { ErrorBoundary } from "./ErrorBoundary";

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/portfolio", label: "Portfolio", icon: FolderGit2 },
  { to: "/projects", label: "Projects", icon: FolderGit2 },
  { to: "/mcp", label: "MCP", icon: Waypoints },
  { to: "/build", label: "Build Control", icon: Play },
  { to: "/agents", label: "Agents", icon: Bot },
  { to: "/backends", label: "Backends", icon: Server },
  { to: "/routing", label: "Routing", icon: RouteIcon },
  { to: "/context", label: "Context", icon: BrainCircuit },
  { to: "/memory", label: "Memory", icon: Database },
  { to: "/knowledge", label: "Knowledge", icon: BookOpen },
  { to: "/skills", label: "Skills", icon: Puzzle },
  { to: "/capacity", label: "Capacity", icon: Gauge },
  { to: "/verification", label: "Verification", icon: ShieldCheck },
  { to: "/activity", label: "Activity", icon: Activity },
  { to: "/settings", label: "Settings", icon: SettingsIcon },
];

export function Shell() {
  const { data: project } = useProject();
  const { data: settings } = useSettings();
  const { connection } = useLive();
  const [paletteOpen, setPaletteOpen] = useState(false);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    const theme = settings?.ui.theme ?? "dark";
    document.documentElement.dataset.theme = theme;
    const reduced = settings?.ui.reduced_motion;
    if (reduced === true) document.documentElement.dataset.motion = "reduced";
    else delete document.documentElement.dataset.motion;
  }, [settings?.ui.theme, settings?.ui.reduced_motion]);

  const scheduler = project?.scheduler;
  const status = schedulerStatus(scheduler?.state, scheduler?.project_status);

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      <nav className="nav-rail" aria-label="Primary">
        <div className="nav-brand">
          <Waypoints size={18} aria-hidden />
          <strong>
            <span>Agentic OS</span>
          </strong>
        </div>
        {NAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={to} end={end} className="nav-link">
            <Icon size={16} aria-hidden />
            <span className="nav-label">{label}</span>
          </NavLink>
        ))}
        <div className="nav-foot">
          local · loopback only
          <br />
          no push · no deploy
        </div>
      </nav>

      <header className="status-strip" role="banner">
        <span className="strip-item" style={{ minWidth: 0 }}>
          <span className="strip-label">Project</span>
          <span className="strip-project">
            {project?.exists ? (project.name ?? "unnamed") : "none"}
          </span>
        </span>
        <span className="strip-item">
          <StatusChip
            status={status}
            pulse={scheduler?.state === "running"}
            title={project?.eligible_reason ?? undefined}
          />
        </span>
        {scheduler?.state === "cooling" && (
          <span className="strip-item">
            <Countdown
              until={scheduler.next_run_at}
              label="Cooling countdown"
            />
          </span>
        )}
        {scheduler?.selected_backend && (
          <span className="strip-item strip-hide-mobile">
            <span className="strip-label">Backend</span>
            <BackendChip name={scheduler.selected_backend} />
          </span>
        )}
        {scheduler?.current_cycle && scheduler.state === "running" && (
          <span className="strip-item strip-hide-mobile">
            <span className="strip-label">Cycle</span>
            <span>{scheduler.current_cycle}</span>
          </span>
        )}
        <span className="strip-spacer" />
        <button
          className="btn strip-hide-mobile"
          onClick={() => setPaletteOpen(true)}
          aria-haspopup="dialog"
          style={{ minHeight: 26 }}
        >
          Commands <kbd className="strip-kbd">Ctrl K</kbd>
        </button>
      </header>

      <main id="main" className="shell-main" tabIndex={-1}>
        {connection === "lost" && (
          <div className="conn-banner" role="status">
            <WifiOff size={13} aria-hidden />
            Live connection lost — reconnecting. Data may be stale.
          </div>
        )}
        <ErrorBoundary>
          <Outlet />
        </ErrorBoundary>
      </main>

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
      <Toasts />
    </div>
  );
}
