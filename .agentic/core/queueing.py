"""Human-approval queue. Every queued item is a JSON file under
.agentic/queue/ containing the work order, the reason, and (when a draft
exists) the worktree/branch/patch to review."""
import datetime as _dt
import json
import os
import re


def enqueue(queue_dir, item):
    os.makedirs(queue_dir, exist_ok=True)
    item = dict(item)
    item.setdefault("queued_at", _dt.datetime.now().isoformat(timespec="seconds"))
    item.setdefault("status", "pending")
    slug = re.sub(r"[^a-z0-9-]+", "-", str(item.get("skill", "task")).lower())[:40]
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(queue_dir, "%s-%s.json" % (stamp, slug))
    n = 1
    while os.path.exists(path):
        path = os.path.join(queue_dir, "%s-%s-%d.json" % (stamp, slug, n))
        n += 1
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(item, fh, indent=2, ensure_ascii=False)
    return path


def list_queue(queue_dir):
    items = []
    if not os.path.isdir(queue_dir):
        return items
    for name in sorted(os.listdir(queue_dir)):
        if name.endswith(".json"):
            with open(os.path.join(queue_dir, name), "r", encoding="utf-8") as fh:
                try:
                    item = json.load(fh)
                except ValueError:
                    item = {"error": "unreadable queue item"}
            item["_file"] = name
            items.append(item)
    return items


def render(queue_dir):
    items = list_queue(queue_dir)
    if not items:
        return "(queue is empty)"
    lines = []
    for item in items:
        lines.append("%s\n  skill:  %s\n  reason: %s\n  branch: %s\n  status: %s"
                     % (item["_file"], item.get("skill", "?"),
                        item.get("reason", "?"), item.get("branch", "-"),
                        item.get("status", "pending")))
    return "\n".join(lines)
