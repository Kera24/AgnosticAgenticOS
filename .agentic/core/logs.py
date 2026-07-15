"""Run logging: decisions.jsonl (append-only audit trail), STATE.md summary,
and alerts. Everything written is redacted first."""
import datetime as _dt
import json
import os
import sys

from .redact import redact


def decision(memory_dir, event):
    """Append one redacted event to decisions.jsonl."""
    os.makedirs(memory_dir, exist_ok=True)
    event = dict(event)
    event.setdefault("ts", _dt.datetime.now().isoformat(timespec="seconds"))
    line = json.dumps(event, ensure_ascii=False, default=str)
    with open(os.path.join(memory_dir, "decisions.jsonl"), "a",
              encoding="utf-8") as fh:
        fh.write(redact(line) + "\n")


def alert(memory_dir, kind, message):
    """MUST ALERT channel: STATE.md, decisions.jsonl, stderr."""
    message = redact(message)
    decision(memory_dir, {"event": "alert", "kind": kind, "message": message})
    state_path = os.path.join(memory_dir, "STATE.md")
    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    with open(state_path, "a", encoding="utf-8") as fh:
        fh.write("\n- **ALERT** [%s] `%s` %s" % (stamp, kind, message))
    print("[ALERT:%s] %s" % (kind, message), file=sys.stderr)


def update_state(memory_dir, run_id, summary_lines):
    state_path = os.path.join(memory_dir, "STATE.md")
    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    with open(state_path, "a", encoding="utf-8") as fh:
        fh.write("\n\n## Run %s (%s)\n" % (run_id, stamp))
        for line in summary_lines:
            fh.write("- %s\n" % redact(line))
