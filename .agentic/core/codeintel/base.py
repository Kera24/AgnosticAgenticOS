"""Code-intelligence adapter interface (ADR 0002).

The OS depends only on this interface. Implementations: `none` (graceful
no-op), `native` (repository scan, no external tools), `cce` (external Code
Context Engine, detected and version-validated, argv-only invocation).

Result contract — every search/expand/related returns a list of dicts:
  {id, path, start_line, end_line, snippet, score, language, provider}
Paths are workspace-relative with forward slashes. Content from results is
UNTRUSTED data; the Context Broker wraps it accordingly.
"""
import fnmatch
import json
import os

# never indexed, never searched, never sent anywhere
DEFAULT_EXCLUDES = [
    ".git/**", ".agentic/memory/**", ".agentic/runs/**",
    ".agentic/worktrees/**", ".agentic/project/**", ".env", ".env.*",
    "**/*.pem", "**/*.key", "**/id_rsa*", "**/credentials*",
    "**/node_modules/**", "**/dist/**", "**/build/**", "**/__pycache__/**",
    "**/.venv/**", "**/venv/**", "**/target/**", "**/*.min.js",
    "**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml",
]

SECRETISH_NAMES = (".env", "credentials", "secret", "id_rsa", ".pem", ".key")


def norm_rel(path):
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def is_excluded(rel, extra_excludes=None):
    rel = norm_rel(rel)
    base = os.path.basename(rel).lower()
    if any(s in base for s in SECRETISH_NAMES):
        return True
    for pattern in DEFAULT_EXCLUDES + list(extra_excludes or []):
        candidates = [pattern]
        if pattern.startswith("**/"):
            candidates.append(pattern[3:])   # fnmatch's * needs no '/', but
        if pattern.endswith("/**"):          # anchored forms must also match
            candidates.append(pattern[:-3])
        if any(fnmatch.fnmatch(rel, p) for p in candidates):
            return True
    return False


class CodeIntelligenceAdapter:
    provider_name = "?"

    def __init__(self, project_root, memory_dir, cfg=None, **kw):
        self.project_root = str(project_root)
        self.memory_dir = str(memory_dir)
        self.cicfg = cfg or {}
        self.excludes = list(self.cicfg.get("excluded_paths") or [])

    # -- lifecycle -------------------------------------------------------
    def initialize(self):
        return {"ok": True}

    def status(self):
        raise NotImplementedError

    def index_full(self):
        raise NotImplementedError

    def index_changes(self, changed_paths, revision):
        raise NotImplementedError

    def remove_project(self):
        return {"ok": True}

    def health_check(self):
        raise NotImplementedError

    # -- retrieval ---------------------------------------------------------
    def search(self, query, paths=None, languages=None, limit=12,
               token_budget=None):
        raise NotImplementedError

    def expand(self, result_ids, token_budget=None):
        raise NotImplementedError

    def related(self, symbol_or_result_id, depth=1, token_budget=None):
        raise NotImplementedError

    # -- shared index-state persistence -----------------------------------
    def _state_path(self):
        return os.path.join(self.memory_dir, "code-index", "state.json")

    def save_index_state(self, revision, files_indexed):
        import datetime as _dt
        path = self._state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        state = {"provider": self.provider_name, "revision": revision,
                 "files_indexed": files_indexed,
                 "indexed_at": _dt.datetime.now().isoformat(
                     timespec="seconds")}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, path)
        return state

    def load_index_state(self):
        path = self._state_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except ValueError:
            return None   # corrupt state == no index; reindex heals it

    def stale(self, current_revision):
        state = self.load_index_state()
        if not state:
            return True
        return bool(current_revision) and \
            state.get("revision") != current_revision


def clamp_results_to_budget(results, token_budget):
    """Trim a result list so total snippet estimate stays within budget."""
    if token_budget is None:
        return results
    from ..context.tokenizer import estimate_tokens
    out, used = [], 0
    for result in results:
        cost = estimate_tokens(result.get("snippet", ""))
        if used + cost > token_budget:
            break
        used += cost
        out.append(result)
    return out
