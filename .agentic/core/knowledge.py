"""Obsidian-compatible Markdown knowledge vault (ADR 0004).

Deterministic writers maintain plain-Markdown documents under
`.agentic/knowledge/` with stable frontmatter. Rules enforced here:

- standard Markdown only; the folder opens as an Obsidian vault but depends
  on nothing Obsidian-specific; `.obsidian/` is never generated or indexed;
- files are rewritten only when generated content actually changed
  (`created` and unchanged `updated` stamps are preserved);
- a clearly-marked user section survives every regeneration;
- a user edit inside the GENERATED area is a conflict: the user's file is
  left untouched and the fresh content lands in `<name>.incoming.md`;
- writes are atomic (tmp + replace); a leftover .tmp never corrupts reads;
- autonomous code never launches Obsidian or any desktop app — `open` is a
  user-invoked CLI action only.
"""
import datetime as _dt
import hashlib
import os
import re

import yaml

from . import projstate
from .redact import redact

USER_START = "<!-- user-notes:start -->"
USER_END = "<!-- user-notes:end -->"
DEFAULT_USER_SECTION = (USER_START +
                        "\n_Your notes here survive regeneration._\n" +
                        USER_END)

FRONTMATTER_FIELDS = ("id", "type", "project", "status", "created",
                      "updated", "source_revision", "tags", "content_hash")

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


def _hash(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def vault_config(cfg):
    raw = cfg.get("knowledge") or {}
    merged = {"enabled": True, "path": None}
    merged.update(raw)
    return merged


class KnowledgeVault:
    def __init__(self, cfg, agentic_dir, clock=None):
        self.cfg = cfg
        self.agentic_dir = str(agentic_dir)
        kcfg = vault_config(cfg)
        self.root = kcfg["path"] or os.path.join(self.agentic_dir,
                                                 "knowledge")
        self.project = (cfg.get("project") or {}).get("name") or "project"
        self.clock = clock or _dt.datetime.now

    # -- low-level document IO ------------------------------------------------

    def path(self, rel):
        rel = rel.replace("\\", "/")
        full = os.path.realpath(os.path.join(self.root, rel))
        root = os.path.realpath(self.root)
        if not (full == root or full.startswith(root + os.sep)):
            raise ValueError("knowledge path escapes the vault: %r" % rel)
        return full

    def read_doc(self, rel):
        """Parse one vault document. Returns None when absent/unreadable."""
        full = self.path(rel)
        if not os.path.exists(full):
            return None
        try:
            with open(full, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            return None
        meta, body = _split_frontmatter(raw)
        if meta is None:
            return {"meta": {}, "generated": body, "user_section": "",
                    "generated_intact": False, "raw": raw}
        if USER_START in body:
            generated, _sep, rest = body.partition(USER_START)
            user_section = USER_START + rest
        else:
            generated, user_section = body, ""
        generated = generated.strip("\n")
        intact = meta.get("content_hash") == _hash(generated)
        return {"meta": meta, "generated": generated,
                "user_section": user_section.strip("\n"),
                "generated_intact": intact, "raw": raw}

    def write_doc(self, rel, doc_id, doc_type, title, body, tags=(),
                  source_revision=None, status="active"):
        """Deterministic write honouring the change/conflict rules above.
        Returns one of: written | unchanged | conflict."""
        body = redact(body).strip("\n")
        generated = "# %s\n\n%s" % (title, body)
        existing = self.read_doc(rel)
        now = self.clock().isoformat(timespec="seconds")

        if existing and existing["meta"]:
            if not existing["generated_intact"]:
                # user edited the generated area: never clobber it
                incoming = os.path.splitext(rel)[0] + ".incoming.md"
                self._atomic_write(incoming, self._render(
                    existing["meta"].get("id", doc_id), doc_type, status,
                    existing["meta"].get("created", now), now,
                    source_revision, tags, generated,
                    DEFAULT_USER_SECTION))
                return "conflict"
            if existing["generated"] == generated:
                return "unchanged"
            created = existing["meta"].get("created", now)
            user_section = existing["user_section"] or DEFAULT_USER_SECTION
        else:
            created = now
            user_section = DEFAULT_USER_SECTION

        self._atomic_write(rel, self._render(
            doc_id, doc_type, status, created, now, source_revision, tags,
            generated, user_section))
        return "written"

    def _render(self, doc_id, doc_type, status, created, updated,
                source_revision, tags, generated, user_section):
        meta = {"id": doc_id, "type": doc_type, "project": self.project,
                "status": status, "created": created, "updated": updated,
                "source_revision": source_revision, "tags": list(tags),
                "content_hash": _hash(generated)}
        front = yaml.safe_dump(meta, sort_keys=False,
                               allow_unicode=True).strip()
        return "---\n%s\n---\n\n%s\n\n%s\n" % (front, generated,
                                               user_section)

    def _atomic_write(self, rel, text):
        full = self.path(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, full)

    # -- deterministic writers -------------------------------------------------

    def rebuild(self):
        """Regenerate every document from persisted project state and
        memory. Idempotent; unchanged files are not touched."""
        a = self.agentic_dir
        results = {}
        revision = None

        plan = _read_text(os.path.join(a, "project", "PROJECT.md"))
        progress = projstate.read_yaml(a, "progress.yaml", {}) or {}
        results["project-overview.md"] = self.write_doc(
            "project-overview.md", "project-overview", "overview",
            "Project Overview",
            (plan or "_No project started yet._")
            + "\n\n## Navigation\n- [[current-state]]\n"
              "- [[architecture/architecture]]\n"
              "- [[requirements/acceptance-criteria]]\n",
            tags=["overview"], source_revision=revision)

        tasks = projstate.load_backlog(a) if projstate.exists(a) else []
        active = [t for t in tasks if t["status"] == "in_progress"]
        state_body = "## Progress\n"
        for key, value in (progress.get("tasks_by_status") or {}).items():
            state_body += "- %s: %s\n" % (key, value)
        state_body += "\n## Milestones\n"
        for mid, mstate in (progress.get("milestones") or {}).items():
            state_body += "- [[milestones/%s|%s]]: %s\n" % (mid, mid, mstate)
        state_body += "\n## Active task\n"
        state_body += ("- %s — %s\n" % (active[0]["id"],
                                        active[0]["description"])
                       if active else "_none_\n")
        results["current-state.md"] = self.write_doc(
            "current-state.md", "current-state", "state", "Current State",
            state_body, tags=["state"])

        arch = _read_text(os.path.join(a, "project", "architecture.md"))
        results["architecture/architecture.md"] = self.write_doc(
            "architecture/architecture.md", "architecture", "architecture",
            "Architecture Map", arch or "_No architecture recorded yet._",
            tags=["architecture"])

        criteria = projstate.read_yaml(a, "acceptance-criteria.yaml", {}) \
            or {}
        req_body = "## Completion criteria\n" + "".join(
            "- %s\n" % c for c in criteria.get("completion_criteria", [])
            or ["_none recorded_"])
        req_map = criteria.get("requirements_map") or []
        if req_map:
            req_body += "\n## Requirements map\n"
            for entry in req_map:
                req_body += "- %s\n" % _as_line(entry)
        results["requirements/acceptance-criteria.md"] = self.write_doc(
            "requirements/acceptance-criteria.md", "acceptance-criteria",
            "requirements", "Requirements and Acceptance Criteria",
            req_body, tags=["requirements"])

        milestones = projstate.read_yaml(a, "milestones.yaml",
                                         {"milestones": []}) or {}
        for milestone in milestones.get("milestones") or []:
            mid = str(milestone.get("id"))
            mtasks = [t for t in tasks if t.get("milestone") == mid]
            body = "%s\n\n## Tasks\n" % (milestone.get("title") or mid) + \
                ("".join("- `%s` (%s): %s\n"
                         % (t["id"], t["status"], t["description"][:120])
                         for t in mtasks) or "_no tasks_\n")
            results["milestones/%s.md" % mid] = self.write_doc(
                "milestones/%s.md" % mid, "milestone-%s" % mid, "milestone",
                "Milestone: %s" % mid, body, tags=["milestone"])

        self._write_memory_docs(results)

        audit = projstate.read_yaml(a, "final-audit.yaml", None)
        if audit:
            body = "Completed: %s\n\n## Checks\n" % audit.get("completed_at")
            for name, ok in (audit.get("checks") or {}).items():
                body += "- %s %s\n" % ("[x]" if ok else "[ ]", name)
            results["audits/final-audit.md"] = self.write_doc(
                "audits/final-audit.md", "final-audit", "audit",
                "Final Audit", body, tags=["audit"])

        self.write_template()
        return results

    def _write_memory_docs(self, results):
        try:
            from .memsvc import get_memory
            memory = get_memory(self.cfg,
                                os.path.join(self.agentic_dir, "memory"))
            decisions = memory.search(
                types=["architecture_decision", "implementation_decision"],
                limit=100)
            cycles = memory.search(types=["cycle_outcome"], limit=30)
        except Exception:
            return
        for row in decisions:
            rel = "decisions/%s.md" % row["id"]
            results[rel] = self.write_doc(
                rel, "decision-%s" % row["id"], "decision", row["title"],
                row["compact_summary"] + "\n\n(see `memory show %s`)"
                % row["id"], tags=["decision", row["type"]])
        if cycles:
            body = "".join("- %s — %s (%s)\n"
                           % (r["title"], r["compact_summary"][:100],
                              r["created_at"]) for r in cycles)
            results["retrospectives/cycles.md"] = self.write_doc(
                "retrospectives/cycles.md", "cycle-retrospective",
                "retrospective", "Cycle Retrospectives", body,
                tags=["retrospective"])

    def write_template(self):
        rel = "templates/note.md"
        if not os.path.exists(self.path(rel)):
            self.write_doc(rel, "template-note", "template", "Note Template",
                           "_Copy this file for manual notes._",
                           tags=["template"])

    # -- validation / status ------------------------------------------------------

    def documents(self):
        out = []
        for base, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if d != ".obsidian"]
            for name in files:
                if name.endswith(".md"):
                    out.append(os.path.relpath(os.path.join(base, name),
                                               self.root).replace("\\", "/"))
        return sorted(out)

    def validate(self):
        issues = []
        docs = self.documents()
        names = {os.path.splitext(os.path.basename(d))[0] for d in docs} | \
                {os.path.splitext(d)[0] for d in docs}
        for rel in docs:
            doc = self.read_doc(rel)
            if doc is None:
                issues.append("%s: unreadable" % rel)
                continue
            if not doc["meta"]:
                if "templates/" in rel:
                    continue
                issues.append("%s: missing frontmatter" % rel)
                continue
            for field in ("id", "type", "project", "created", "updated"):
                if not doc["meta"].get(field):
                    issues.append("%s: frontmatter missing %r" % (rel, field))
            if not doc["generated_intact"]:
                issues.append("%s: generated section edited by user "
                              "(conflict pending)" % rel)
            for match in _WIKILINK_RE.finditer(doc["generated"]
                                               + doc["user_section"]):
                target = match.group(1).strip()
                if target and target not in names and \
                        os.path.splitext(os.path.basename(target))[0] \
                        not in names:
                    issues.append("%s: broken link [[%s]]" % (rel, target))
        return issues

    def status(self):
        docs = self.documents()
        conflicts = [d for d in docs if d.endswith(".incoming.md")]
        return {"root": self.root, "documents": len(docs),
                "conflicts": conflicts,
                "obsidian_workspace_present":
                    os.path.exists(os.path.join(self.root, ".obsidian")),
                "issues": self.validate()}


def update_knowledge(cfg, agentic_dir, log=None):
    """Best-effort vault refresh hook for cycles/audits."""
    if not vault_config(cfg)["enabled"]:
        return None
    try:
        results = KnowledgeVault(cfg, agentic_dir).rebuild()
        if log:
            written = [r for r, s in results.items() if s == "written"]
            log({"event": "knowledge_updated", "written": len(written),
                 "conflicts": [r for r, s in results.items()
                               if s == "conflict"]})
        return results
    except Exception as exc:   # noqa: BLE001 — the vault never breaks a cycle
        if log:
            log({"event": "knowledge_update_failed",
                 "detail": str(exc)[:200]})
        return None


def knowledge_items(cfg, agentic_dir, query, limit=4):
    """Relevant vault SECTIONS as untrusted ContextItems (never whole
    files, never the workspace folder)."""
    if not vault_config(cfg)["enabled"] or not query:
        return []
    from .context.items import ContextItem
    try:
        vault = KnowledgeVault(cfg, agentic_dir)
        terms = [t.lower() for t in re.findall(r"[A-Za-z_]{3,}", query)]
        scored = []
        for rel in vault.documents():
            if rel.endswith(".incoming.md") or rel.startswith("templates/"):
                continue
            doc = vault.read_doc(rel)
            if not doc:
                continue
            for section in _sections(doc["generated"]):
                low = section.lower()
                score = sum(low.count(t) for t in terms)
                if score:
                    scored.append((score, rel, section))
        scored.sort(key=lambda s: -s[0])
        return [ContextItem("knowledge", section[:4000],
                            source_type="knowledge", source_path=rel,
                            relevance_score=min(0.8, 0.4 + score / 20.0),
                            trust_level="untrusted")
                for score, rel, section in scored[:limit]]
    except Exception:   # noqa: BLE001
        return []


def _sections(markdown):
    parts, current = [], []
    for line in markdown.splitlines():
        if line.startswith("#") and current:
            parts.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        parts.append("\n".join(current))
    return parts


def _split_frontmatter(raw):
    if not raw.startswith("---"):
        return None, raw
    end = raw.find("\n---", 3)
    if end < 0:
        return None, raw
    try:
        meta = yaml.safe_load(raw[3:end]) or {}
    except yaml.YAMLError:
        return None, raw
    body = raw[end + 4:].lstrip("\n")
    return (meta if isinstance(meta, dict) else None), body


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _as_line(entry):
    if isinstance(entry, dict):
        return "; ".join("%s: %s" % (k, v) for k, v in entry.items())
    return str(entry)
