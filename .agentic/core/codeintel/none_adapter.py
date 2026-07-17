"""`none` adapter: code intelligence disabled or unavailable. Every
operation succeeds with an honest empty result so the OS keeps its basic
(snapshot-based) behaviour."""
from .base import CodeIntelligenceAdapter


class NoneAdapter(CodeIntelligenceAdapter):
    provider_name = "none"

    def status(self):
        return {"provider": "none", "indexed": False, "revision": None,
                "detail": "code intelligence disabled; snapshot context only"}

    def index_full(self):
        return {"ok": True, "files_indexed": 0, "provider": "none"}

    def index_changes(self, changed_paths, revision):
        return {"ok": True, "files_indexed": 0, "provider": "none"}

    def health_check(self):
        return {"ok": True, "provider": "none"}

    def search(self, query, paths=None, languages=None, limit=12,
               token_budget=None):
        return []

    def expand(self, result_ids, token_budget=None):
        return []

    def related(self, symbol_or_result_id, depth=1, token_budget=None):
        return []
