"""OS-owned persistent memory with progressive disclosure (ADR 0003).

Three layers, mirroring claude-mem's model without its auto-injection:
  1. search()   -> compact rows (id, type, title, summary)
  2. timeline() -> events surrounding one record
  3. details()  -> full records for explicitly chosen ids

Rules enforced here:
- everything is redacted at write time; credentials never enter the store;
- records are project-isolated; queries always filter project_id;
- superseded/expired records are never returned by default and never
  injected into prompts;
- reviewer-verified records rank above unverified ones; the current project
  plan always outranks memory (the broker's category ordering guarantees
  this: memory is optional content, the work order is mandatory);
- memory is data, not instructions — injected items are UNTRUSTED;
- a corrupt database is moved aside and recreated, never fatal.

Storage: SQLite at .agentic/memory/memory.db (stdlib sqlite3, WAL mode for
interrupted-write safety).
"""
import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import uuid

from .redact import redact

RECORD_TYPES = [
    "requirement", "constraint", "preference", "architecture_decision",
    "implementation_decision", "failed_attempt", "bug", "resolution",
    "reviewer_finding", "security_finding", "cycle_outcome",
    "milestone_outcome", "project_summary",
]

STATUSES = ("active", "superseded", "expired")

DB_NAME = "memory.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  cycle_id TEXT, task_id TEXT,
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  compact_summary TEXT NOT NULL,
  details TEXT,
  source TEXT, source_revision TEXT,
  confidence REAL DEFAULT 0.5,
  importance REAL DEFAULT 0.5,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  valid_from TEXT, expires_at TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  supersedes TEXT,
  tags TEXT, related_paths TEXT, related_symbols TEXT,
  reviewer_verified INTEGER DEFAULT 0,
  sensitive INTEGER DEFAULT 0,
  fingerprint TEXT
);
CREATE INDEX IF NOT EXISTS idx_records_project
  ON records (project_id, status, type);
CREATE INDEX IF NOT EXISTS idx_records_fp ON records (fingerprint);
"""

_LIST_FIELDS = ("tags", "related_paths", "related_symbols")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")


def _now():
    return _dt.datetime.now()


class MemoryService:
    def __init__(self, memory_dir, project_id, clock=None):
        self.memory_dir = str(memory_dir)
        self.project_id = str(project_id)
        self.clock = clock or _now
        self.path = os.path.join(self.memory_dir, DB_NAME)
        self._conn = None

    # -- connection & recovery ------------------------------------------------
    def conn(self):
        if self._conn is not None:
            return self._conn
        os.makedirs(self.memory_dir, exist_ok=True)
        conn = None
        try:
            conn = sqlite3.connect(self.path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        except sqlite3.DatabaseError:
            # corrupt database: preserve the bytes for forensics, recreate
            if conn is not None:
                conn.close()   # Windows: release the handle before rename
            stamp = self.clock().strftime("%Y%m%d-%H%M%S")
            try:
                os.replace(self.path, self.path + ".corrupt-" + stamp)
            except OSError:
                os.remove(self.path)
            conn = sqlite3.connect(self.path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        conn.row_factory = sqlite3.Row
        self._conn = conn
        return conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- writing ---------------------------------------------------------------
    def save(self, type, title, compact_summary, details=None, task_id=None,
             cycle_id=None, source=None, source_revision=None,
             confidence=0.5, importance=0.5, expires_at=None,
             supersedes=None, tags=None, related_paths=None,
             related_symbols=None, reviewer_verified=False, sensitive=False):
        """Insert (or refresh) one record. Deterministic: identical
        type+title+summary content updates the existing record instead of
        duplicating it. All text is redacted before touching disk."""
        if type not in RECORD_TYPES:
            raise ValueError("unknown memory record type %r" % type)
        title = redact(str(title))[:300]
        compact_summary = redact(str(compact_summary))[:600]
        details = redact(str(details)) if details is not None else None
        now = self.clock().isoformat(timespec="seconds")
        fingerprint = hashlib.sha256(
            ("%s|%s|%s|%s" % (self.project_id, type, title, compact_summary))
            .encode("utf-8", "replace")).hexdigest()[:24]
        conn = self.conn()
        existing = conn.execute(
            "SELECT id FROM records WHERE fingerprint=? AND project_id=? "
            "AND status='active'", (fingerprint, self.project_id)).fetchone()
        if existing:
            conn.execute(
                "UPDATE records SET updated_at=?, details=COALESCE(?,details),"
                " confidence=?, importance=?, reviewer_verified=? WHERE id=?",
                (now, details, confidence, importance,
                 int(bool(reviewer_verified)), existing["id"]))
            conn.commit()
            return existing["id"]
        record_id = uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO records (id, project_id, cycle_id, task_id, type,"
            " title, compact_summary, details, source, source_revision,"
            " confidence, importance, created_at, updated_at, valid_from,"
            " expires_at, status, supersedes, tags, related_paths,"
            " related_symbols, reviewer_verified, sensitive, fingerprint)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record_id, self.project_id, cycle_id, task_id, type, title,
             compact_summary, details, source, source_revision,
             float(confidence), float(importance), now, now, now,
             expires_at, "active", supersedes,
             json.dumps(tags or []), json.dumps(related_paths or []),
             json.dumps(related_symbols or []),
             int(bool(reviewer_verified)), int(bool(sensitive)),
             fingerprint))
        if supersedes:
            conn.execute(
                "UPDATE records SET status='superseded', updated_at=? "
                "WHERE id=? AND project_id=?",
                (now, supersedes, self.project_id))
        conn.commit()
        return record_id

    def forget(self, record_id):
        """Hard delete (user-invoked)."""
        conn = self.conn()
        cur = conn.execute("DELETE FROM records WHERE id=? AND project_id=?",
                           (record_id, self.project_id))
        conn.commit()
        return cur.rowcount

    def expire_due(self):
        now = self.clock().isoformat(timespec="seconds")
        conn = self.conn()
        cur = conn.execute(
            "UPDATE records SET status='expired', updated_at=? WHERE "
            "project_id=? AND status='active' AND expires_at IS NOT NULL "
            "AND expires_at < ?", (now, self.project_id, now))
        conn.commit()
        return cur.rowcount

    def compact(self, retention_days=180):
        """Delete superseded/expired records past retention; vacuum."""
        cutoff = (self.clock() - _dt.timedelta(days=retention_days)) \
            .isoformat(timespec="seconds")
        self.expire_due()
        conn = self.conn()
        cur = conn.execute(
            "DELETE FROM records WHERE project_id=? AND status IN "
            "('superseded','expired') AND updated_at < ?",
            (self.project_id, cutoff))
        conn.commit()
        conn.execute("VACUUM")
        return cur.rowcount

    # -- reading (progressive disclosure) ------------------------------------------
    def search(self, query=None, types=None, task_id=None, limit=20,
               include_superseded=False, include_sensitive=False):
        """Layer 1: compact rows only."""
        self.expire_due()
        conn = self.conn()
        sql = ("SELECT id, type, title, compact_summary, importance,"
               " confidence, reviewer_verified, status, created_at, task_id"
               " FROM records WHERE project_id=?")
        params = [self.project_id]
        if not include_superseded:
            sql += " AND status='active'"
        if not include_sensitive:
            sql += " AND sensitive=0"
        if types:
            sql += " AND type IN (%s)" % ",".join("?" * len(types))
            params.extend(types)
        if task_id:
            sql += " AND task_id=?"
            params.append(task_id)
        rows = [dict(r) for r in conn.execute(sql, params)]
        if query:
            terms = [t.lower() for t in _WORD_RE.findall(query)]
            scored = []
            for row in rows:
                haystack = ("%s %s" % (row["title"],
                                       row["compact_summary"])).lower()
                score = sum(haystack.count(t) for t in terms)
                if score:
                    scored.append((score, row))
            scored.sort(key=lambda pair: (-pair[0],
                                          pair[1]["created_at"]))
            rows = [row for _s, row in scored]
        else:
            rows.sort(key=lambda r: (-(r["reviewer_verified"] or 0),
                                     -(r["importance"] or 0),
                                     r["created_at"]), )
        return rows[:limit]

    def timeline(self, record_id, window=5):
        """Layer 2: the record plus surrounding events of the project."""
        conn = self.conn()
        anchor = conn.execute(
            "SELECT * FROM records WHERE id=? AND project_id=?",
            (record_id, self.project_id)).fetchone()
        if anchor is None:
            return []
        rows = conn.execute(
            "SELECT id, type, title, compact_summary, created_at, status"
            " FROM records WHERE project_id=? ORDER BY created_at",
            (self.project_id,)).fetchall()
        ids = [r["id"] for r in rows]
        pos = ids.index(record_id)
        lo, hi = max(0, pos - window), min(len(rows), pos + window + 1)
        return [dict(r) for r in rows[lo:hi]]

    def details(self, record_ids):
        """Layer 3: full records for explicitly chosen ids."""
        conn = self.conn()
        out = []
        for rid in record_ids:
            row = conn.execute(
                "SELECT * FROM records WHERE id=? AND project_id=?",
                (rid, self.project_id)).fetchone()
            if row is None:
                continue
            record = dict(row)
            for field in _LIST_FIELDS:
                try:
                    record[field] = json.loads(record.get(field) or "[]")
                except ValueError:
                    record[field] = []
            out.append(record)
        return out

    def status(self):
        conn = self.conn()
        by = {}
        for row in conn.execute(
                "SELECT type, status, COUNT(*) AS n FROM records WHERE "
                "project_id=? GROUP BY type, status", (self.project_id,)):
            by.setdefault(row["type"], {})[row["status"]] = row["n"]
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM records WHERE project_id=?",
            (self.project_id,)).fetchone()["n"]
        return {"project_id": self.project_id, "path": self.path,
                "total_records": total, "by_type": by}

    def export(self, out_path):
        """Full project export as JSON (sensitive records included — the
        export is a local file the user explicitly asked for)."""
        conn = self.conn()
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM records WHERE project_id=? ORDER BY created_at",
            (self.project_id,))]
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump({"project_id": self.project_id, "records": rows},
                      fh, indent=2, default=str)
        return len(rows)


def memory_config(cfg):
    raw = cfg.get("memory") or {}
    merged = {"enabled": True, "retention_days": 180, "inject_limit": 8}
    merged.update(raw)
    return merged


def get_memory(cfg, memory_dir, clock=None):
    project = (cfg.get("project") or {}).get("name") or "default"
    return MemoryService(memory_dir, project, clock=clock)


def memory_items(cfg, memory_dir, query, clock=None):
    """Token-eligible ContextItems from memory for the broker: compact
    summaries only (progressive disclosure), active records only, sensitive
    records never injected, bounded by memory.inject_limit — never the full
    history."""
    mcfg = memory_config(cfg)
    if not mcfg["enabled"] or not query:
        return []
    from .context.items import ContextItem
    try:
        service = get_memory(cfg, memory_dir, clock=clock)
        rows = service.search(query, limit=int(mcfg["inject_limit"]))
    except Exception:   # memory must never break an invocation
        return []
    items = []
    for row in rows:
        text = "[%s %s] %s — %s" % (row["type"], row["id"], row["title"],
                                    row["compact_summary"])
        items.append(ContextItem(
            "memory", text, source_type="memory", source_path=row["id"],
            relevance_score=min(0.85, 0.4
                                + 0.15 * (row["reviewer_verified"] or 0)
                                + 0.3 * float(row["importance"] or 0)),
            trust_level="untrusted",
            metadata={"type": row["type"],
                      "reviewer_verified": bool(row["reviewer_verified"])}))
    return items
