"""`native` adapter: dependency-free retrieval over tracked files.

Pure Python term scoring (no shell, no subprocess beyond `git ls-files`):
tokenizes the query, scores each candidate line window by term hits, and
returns bounded snippets. Not semantic — an honest fallback that keeps the
OS retrieval-capable when CCE is not installed.
"""
import os
import re

from .. import gitops
from .base import (CodeIntelligenceAdapter, clamp_results_to_budget,
                   is_excluded, norm_rel)

TEXT_EXTENSIONS = (
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".txt", ".html", ".css", ".sql", ".sh", ".ps1",
    ".go", ".rs", ".java", ".rb", ".c", ".h", ".cpp", ".cs")
MAX_FILE_BYTES = 400_000
SNIPPET_CONTEXT_LINES = 6

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


class NativeAdapter(CodeIntelligenceAdapter):
    provider_name = "native"

    # -- lifecycle ------------------------------------------------------------
    def _tracked_files(self):
        try:
            listing = gitops.run_git(["ls-files"], cwd=self.project_root,
                                     check=False).splitlines()
        except Exception:
            listing = []
        if not listing:   # not a git repo: bounded walk
            listing = []
            for base, dirs, files in os.walk(self.project_root):
                dirs[:] = [d for d in dirs if d not in
                           (".git", "node_modules", "__pycache__", ".venv")]
                for name in files:
                    rel = norm_rel(os.path.relpath(
                        os.path.join(base, name), self.project_root))
                    listing.append(rel)
                if len(listing) > 5000:
                    break
        return [norm_rel(f) for f in listing
                if f and not is_excluded(f, self.excludes)
                and f.lower().endswith(TEXT_EXTENSIONS)]

    def _revision(self):
        try:
            return gitops.run_git(["rev-parse", "HEAD"],
                                  cwd=self.project_root,
                                  check=False).strip() or None
        except Exception:
            return None

    def index_full(self):
        files = self._tracked_files()
        state = self.save_index_state(self._revision(), len(files))
        return {"ok": True, "provider": "native",
                "files_indexed": len(files), "revision": state["revision"]}

    def index_changes(self, changed_paths, revision):
        # native scans live files; only the revision marker needs updating
        state = self.load_index_state() or {"files_indexed": 0}
        self.save_index_state(revision or self._revision(),
                              state.get("files_indexed", 0))
        return {"ok": True, "provider": "native",
                "files_indexed": len(changed_paths or [])}

    def status(self):
        state = self.load_index_state()
        revision = self._revision()
        return {"provider": "native", "indexed": state is not None,
                "revision": (state or {}).get("revision"),
                "stale": self.stale(revision),
                "files_indexed": (state or {}).get("files_indexed"),
                "indexed_at": (state or {}).get("indexed_at")}

    def health_check(self):
        ok = os.path.isdir(self.project_root)
        return {"ok": ok, "provider": "native",
                "detail": None if ok else "project root missing"}

    # -- retrieval ----------------------------------------------------------
    def search(self, query, paths=None, languages=None, limit=12,
               token_budget=None):
        terms = [t.lower() for t in _WORD_RE.findall(query or "")]
        if not terms:
            return []
        results = []
        for rel in self._tracked_files():
            if paths and not any(rel.startswith(norm_rel(p).rstrip("*/"))
                                 for p in paths):
                continue
            full = os.path.join(self.project_root, rel)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
                with open(full, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue
            best = _best_window(lines, terms)
            if best is None:
                continue
            start, end, score = best
            snippet = "\n".join(lines[start:end])
            results.append({
                "id": "native:%s:%d-%d" % (rel, start + 1, end),
                "path": rel, "start_line": start + 1, "end_line": end,
                "snippet": snippet, "score": round(score, 3),
                "language": os.path.splitext(rel)[1].lstrip("."),
                "provider": "native"})
        results.sort(key=lambda r: (-r["score"], r["path"]))
        return clamp_results_to_budget(results[:limit], token_budget)

    def expand(self, result_ids, token_budget=None):
        out = []
        for rid in result_ids or []:
            parsed = _parse_id(rid)
            if not parsed:
                continue
            rel, start, end = parsed
            if is_excluded(rel, self.excludes):
                continue
            full = os.path.join(self.project_root, rel)
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue
            lo = max(0, start - 1 - 20)
            hi = min(len(lines), end + 20)
            out.append({"id": "native:%s:%d-%d" % (rel, lo + 1, hi),
                        "path": rel, "start_line": lo + 1, "end_line": hi,
                        "snippet": "\n".join(lines[lo:hi]), "score": 1.0,
                        "language": os.path.splitext(rel)[1].lstrip("."),
                        "provider": "native"})
        return clamp_results_to_budget(out, token_budget)

    def related(self, symbol_or_result_id, depth=1, token_budget=None):
        symbol = symbol_or_result_id
        parsed = _parse_id(symbol_or_result_id)
        if parsed:
            symbol = os.path.splitext(os.path.basename(parsed[0]))[0]
        return self.search(symbol, limit=6, token_budget=token_budget)


def _parse_id(rid):
    if not (rid or "").startswith("native:"):
        return None
    try:
        _, rel, span = rid.split(":", 2)
        start, end = span.split("-")
        return norm_rel(rel), int(start), int(end)
    except ValueError:
        return None


def _best_window(lines, terms, window=SNIPPET_CONTEXT_LINES * 2):
    """Highest term-density window of `window` lines. Returns
    (start, end, score) or None when no term matches at all."""
    lowered = [ln.lower() for ln in lines]
    hits = []
    for i, line in enumerate(lowered):
        count = sum(line.count(t) for t in terms)
        if count:
            hits.append((i, count))
    if not hits:
        return None
    best_start, best_score = 0, -1.0
    for (i, _c) in hits:
        start = max(0, i - SNIPPET_CONTEXT_LINES)
        end = min(len(lines), start + window)
        score = sum(c for j, c in hits if start <= j < end)
        distinct = len({t for t in terms
                        if any(t in lowered[j] for j, _ in hits
                               if start <= j < end)})
        score = score + distinct * 2
        if score > best_score:
            best_start, best_score = start, score
    end = min(len(lines), best_start + window)
    return best_start, end, float(best_score)
