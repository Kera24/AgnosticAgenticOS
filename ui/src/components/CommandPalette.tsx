/* Keyboard command palette (Ctrl/Cmd-K). Navigation + safe project actions.
   Invalid actions stay listed but disabled, with the reason shown. */
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { CornerDownLeft } from "lucide-react";
import { useProject, useProjectAction } from "../state/queries";
import { useLive } from "../state/events";

interface Command {
  id: string;
  label: string;
  hint?: string;
  disabled?: string | null;
  run: () => void;
}

export function CommandPalette({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const { data: project, refetch } = useProject();
  const { pushToast } = useLive();
  const run = useProjectAction("run");
  const pause = useProjectAction("pause");
  const resume = useProjectAction("resume");
  const review = useProjectAction("review");
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);

  const state = project?.scheduler.state;
  const exists = Boolean(project?.exists);

  const commands = useMemo<Command[]>(() => {
    const go = (to: string) => () => {
      navigate(to);
      onClose();
    };
    const act =
      (
        mutation: ReturnType<typeof useProjectAction>,
        title: string,
      ) =>
      () => {
        mutation.mutate(undefined, {
          onError: (error) =>
            pushToast({ tone: "fail", title, detail: error.message, sticky: true }),
        });
        onClose();
      };
    return [
      { id: "overview", label: "Open overview", run: go("/") },
      { id: "project", label: "Open current project", run: go("/projects") },
      {
        id: "start-cycle",
        label: "Start eligible cycle",
        hint: "runs one development cycle",
        disabled: !exists
          ? "no project started"
          : state === "running"
            ? "a cycle is already running"
            : state === "paused"
              ? "project is paused — resume first"
              : state === "complete"
                ? "project is complete"
                : null,
        run: act(run, "Start cycle failed"),
      },
      {
        id: "pause",
        label: "Pause project",
        disabled:
          !exists || state === "paused" || state === "complete"
            ? "not pausable now"
            : null,
        run: act(pause, "Pause failed"),
      },
      {
        id: "resume",
        label: "Resume project",
        disabled: state !== "paused" ? "project is not paused" : null,
        run: act(resume, "Resume failed"),
      },
      {
        id: "audit",
        label: "Run final audit",
        disabled: !exists ? "no project started" : null,
        run: act(review, "Final audit failed"),
      },
      {
        id: "refresh",
        label: "Refresh status",
        run: () => {
          refetch();
          onClose();
        },
      },
      { id: "backends", label: "View backends", run: go("/backends") },
      { id: "capacity", label: "View capacity", run: go("/capacity") },
      {
        id: "blockers",
        label: "View blockers",
        hint: `${project?.blockers.length ?? 0} open`,
        run: go("/build"),
      },
      { id: "settings", label: "Open settings", run: go("/settings") },
    ];
  }, [exists, state, navigate, onClose, run, pause, resume, review, refetch, project?.blockers.length, pushToast]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter((c) => c.label.toLowerCase().includes(q));
  }, [commands, query]);

  useEffect(() => {
    if (open) {
      previousFocus.current = document.activeElement as HTMLElement;
      setQuery("");
      setIndex(0);
      window.setTimeout(() => inputRef.current?.focus(), 0);
    } else {
      previousFocus.current?.focus();
    }
  }, [open]);

  useEffect(() => setIndex(0), [query]);

  if (!open) return null;

  const onKey = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") onClose();
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setIndex((i) => Math.min(i + 1, filtered.length - 1));
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setIndex((i) => Math.max(i - 1, 0));
    }
    if (event.key === "Enter") {
      const command = filtered[index];
      if (command && !command.disabled) command.run();
    }
  };

  return (
    <div
      className="overlay"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        className="palette"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onKeyDown={onKey}
      >
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Type a command…"
          aria-label="Search commands"
          role="combobox"
          aria-expanded="true"
          aria-controls="palette-list"
          aria-activedescendant={
            filtered[index] ? `cmd-${filtered[index].id}` : undefined
          }
        />
        <ul className="palette-list" id="palette-list" role="listbox">
          {filtered.length === 0 && (
            <li className="palette-item disabled">No matching command</li>
          )}
          {filtered.map((command, i) => (
            <li
              key={command.id}
              id={`cmd-${command.id}`}
              role="option"
              aria-selected={i === index}
              aria-disabled={Boolean(command.disabled)}
              className={`palette-item${command.disabled ? " disabled" : ""}`}
              onMouseEnter={() => setIndex(i)}
              onClick={() => !command.disabled && command.run()}
            >
              <span className="pi-label">{command.label}</span>
              {command.disabled ? (
                <span className="pi-why">{command.disabled}</span>
              ) : (
                command.hint && <span className="pi-why">{command.hint}</span>
              )}
              {i === index && !command.disabled && (
                <kbd aria-hidden>
                  <CornerDownLeft size={10} />
                </kbd>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
