"""Local notifications. Channels: terminal (always), a file-based inbox
(always, so nothing is lost), and a best-effort desktop notification per
platform. No email/Slack/external messaging unless a user explicitly wires
it in later.

Interaction-mode policy lives in should_notify(): in completion_only mode,
per-cycle events are suppressed and only the documented triggers notify.
"""
import datetime as _dt
import os
import platform
import sys

from . import execpolicy

# events that always notify regardless of interaction mode
CRITICAL_EVENTS = {"project_complete", "human_blocker", "backends_unavailable",
                   "security_decision", "status_requested"}
CYCLE_EVENTS = {"cycle_complete", "milestone_complete"}


def should_notify(cfg, event):
    mode = (cfg.get("interaction") or {}).get("mode", "completion_only") \
        if isinstance(cfg.get("interaction"), dict) \
        else cfg.get("interaction", "completion_only")
    if event in CRITICAL_EVENTS:
        return True
    if event == "cycle_complete":
        return mode == "cycle_review"
    if event == "milestone_complete":
        return mode in ("cycle_review", "milestone_review")
    return False


def notify(cfg, event, title, message, memory_dir):
    """Deliver a notification if the interaction mode allows it. Returns
    True when delivered."""
    if not should_notify(cfg, event):
        return False
    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    line = "[%s] %s: %s - %s" % (stamp, event, title, message)
    print("\n*** %s ***\n" % line, file=sys.stderr)
    # file-based inbox (always on when notifying)
    os.makedirs(memory_dir, exist_ok=True)
    with open(os.path.join(memory_dir, "notifications.log"), "a",
              encoding="utf-8") as fh:
        fh.write(line + "\n")
    prefs = (cfg.get("notifications") or {})
    if prefs.get("desktop", True):
        _desktop(title, message)
    return True


def _desktop(title, message):
    """Best-effort desktop notification; failures are silent by design."""
    system = platform.system()
    try:
        if system == "Windows":
            script = ("[reflection.assembly]::loadwithpartialname("
                      "'System.Windows.Forms') | Out-Null; "
                      "$n = New-Object System.Windows.Forms.NotifyIcon; "
                      "$n.Icon = [System.Drawing.SystemIcons]::Information; "
                      "$n.Visible = $true; "
                      "$n.ShowBalloonTip(10000, %r, %r, 'Info')"
                      % (title[:60], message[:200]))
            execpolicy.run_command(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 script], cwd=".", timeout=20, source="config")
        elif system == "Darwin":
            execpolicy.run_command(
                ["osascript", "-e",
                 'display notification "%s" with title "%s"'
                 % (message[:200].replace('"', "'"),
                    title[:60].replace('"', "'"))],
                cwd=".", timeout=20, source="config")
        else:
            execpolicy.run_command(
                ["notify-send", title[:60], message[:200]],
                cwd=".", timeout=20, source="config")
    except Exception:
        pass
