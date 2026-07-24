"""Provider-neutral prompt/context cache (Phase 2).

A content-addressed store under `.agentic/memory/cache/` for two kinds
of content:

- artifact cache: expensive-to-recompute STABLE content (repository
  inventory, repository map, architecture/requirements summaries, task
  dependency summaries, skill documentation extracts, MCP tool
  descriptions, deterministic-check summaries, stable project context
  packages, rendered prompt-prefix text).
- result cache: ONLY the explicit allowlist of deterministic, reusable,
  VALIDATED outputs in `RESULT_CATEGORIES` -- generated code and any
  review/approval verdict can NEVER be stored here (`FORBIDDEN_CATEGORIES`
  raises `CacheError` unconditionally; there is no override).

This is entirely separate from, and additive to, the PROVIDER-NATIVE
caching that already existed before this module (Anthropic
`cache_control`, OpenAI automatic prefix caching -- see
`providers/anthropic.py`, `providers/openai.py`, and
`core.context.broker`'s `CACHE_BOUNDARY`). Provider-native caching
happens on the wire, per call, and is reported BY the provider; this
module avoids redoing LOCAL work (re-reading files, re-rendering stable
text, re-computing summaries) across calls, regardless of which
provider or CLI backend is in use -- CLI/subscription backends get real
benefit here even though they can never claim provider-side caching."""
import datetime as _dt
import hashlib
import json
import os

from .redact import looks_like_secret

SCHEMA_VERSION = "1.0"

CACHE_DIRNAME = "cache"
TELEMETRY_FILENAME = "cache-telemetry.jsonl"

ARTIFACT_CATEGORIES = (
    "repository_inventory", "repository_map", "architecture_summary",
    "requirements_summary", "task_dependency_summary",
    "skill_documentation_extract", "mcp_tool_description",
    "deterministic_check_summary", "stable_project_context_package",
    "prompt_prefix_text",
)
# Result cache (item A.3): the ONLY categories ever eligible -- every one
# is a deterministic, code-computed fact, never a model's own output.
RESULT_CATEGORIES = (
    "repository_inventory", "repository_map", "tooling_detection",
    "skill_documentation_extract", "mcp_tool_description",
    "deterministic_check_summary",
)
# Explicitly, permanently forbidden regardless of caller intent: generated
# code and any review/approval verdict must always be freshly produced
# and freshly reviewed, never reused from a prior run.
FORBIDDEN_CATEGORIES = (
    "generated_code", "review_verdict", "qa_verdict", "security_verdict",
    "worker_output", "completion_decision",
)

_ALL_ALLOWED_CATEGORIES = set(ARTIFACT_CATEGORIES) | set(RESULT_CATEGORIES)

# Path/key-shaped denylist: refuse to cache anything whose declared
# source/category hint matches one of these, regardless of what a
# content scan finds -- belt and braces alongside looks_like_secret().
_SENSITIVE_HINT_MARKERS = (
    "credential", "token", "session", "auth-verification", "model-registry",
    ".env", "secret", "config.machine.yaml", ".pem", ".key", "password",
)


class CacheError(Exception):
    pass


def _cache_dir(memory_dir):
    return os.path.join(memory_dir, CACHE_DIRNAME)


def _entry_path(memory_dir, key):
    return os.path.join(_cache_dir(memory_dir), key + ".json")


def _canonical(value):
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def compute_cache_key(**components):
    """Content-addressed key over every identity component (item B):
    schema_version, provider, backend_mode, model, role,
    prompt_template_version, task_contract_hash, stable_prefix_hash,
    repository_revision, relevant_file_hashes, inventory_version,
    tool_manifest_hash, skill_set_hash, security_policy_hash,
    output_schema_hash. Callers pass only what applies -- omitted
    components are simply absent from the hashed payload, so the key
    still narrows correctly on whatever WAS supplied. Always
    deterministic: same components -> same key, every time."""
    payload = {"schema_version": SCHEMA_VERSION}
    payload.update({k: v for k, v in components.items() if v is not None})
    return hashlib.sha256(
        _canonical(payload).encode("utf-8")).hexdigest()[:32]


def hash_text(text):
    return hashlib.sha256(
        (text or "").encode("utf-8", "replace")).hexdigest()[:16]


def hash_file(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except OSError:
        return None


def _is_sensitive(hint, text_repr):
    low = (hint or "").lower()
    if any(marker in low for marker in _SENSITIVE_HINT_MARKERS):
        return True
    return looks_like_secret(text_repr)


class CacheStore:
    def __init__(self, memory_dir, clock=None):
        self.memory_dir = memory_dir
        self.clock = clock or _dt.datetime.now

    # -- writing ------------------------------------------------------------

    def put_artifact(self, key, value, category, dependencies=None,
                     ttl_seconds=None, tokens_estimated=None,
                     source_hint=None):
        """Artifact cache (item A.1/A.2): any of ARTIFACT_CATEGORIES."""
        if category in FORBIDDEN_CATEGORIES:
            raise CacheError(
                "category %r may never be cached -- generated code and "
                "review verdicts are always freshly produced, never "
                "reused" % category)
        if category not in _ALL_ALLOWED_CATEGORIES:
            raise CacheError("unknown cache category: %r" % category)
        text_repr = value if isinstance(value, str) else _canonical(value)
        if _is_sensitive(source_hint or category, text_repr):
            raise CacheError(
                "refusing to cache content that looks sensitive "
                "(hint=%r)" % (source_hint or category))
        entry = {"key": key, "category": category, "value": value,
                 "dependencies": dependencies or {},
                 "created_at": self.clock().isoformat(timespec="seconds"),
                 "ttl_seconds": ttl_seconds,
                 "tokens_estimated": tokens_estimated}
        self._write(key, entry)
        return entry

    def put_result(self, key, value, category, dependencies=None, **kw):
        """Result cache (item A.3): a STRICT subset of the artifact
        categories -- deterministic/reusable/validated facts only.
        `RESULT_CATEGORIES` never includes generated content or a
        review verdict; this is enforced by `put_artifact`'s own
        category check regardless, but this entry point makes the
        intent explicit and rejects anything outside the narrower
        result-cache allowlist even if it happens to also be a valid
        artifact category."""
        if category not in RESULT_CATEGORIES:
            raise CacheError(
                "category %r is not a result-cache category (allowed: "
                "%s)" % (category, ", ".join(RESULT_CATEGORIES)))
        return self.put_artifact(key, value, category,
                                 dependencies=dependencies, **kw)

    # -- reading --------------------------------------------------------------

    def get(self, key, category=None):
        """Returns (value, hit: bool). Always records telemetry --
        callers never need to log hit/miss themselves."""
        entry = self._read(key)
        if entry is None or (category and entry.get("category") != category):
            self._telemetry(key, "application_cache_miss",
                            category or (entry or {}).get("category"))
            return None, False
        self._telemetry(key, "application_cache_hit", entry.get("category"),
                        tokens_estimated=entry.get("tokens_estimated"))
        return entry.get("value"), True

    # -- invalidation ---------------------------------------------------------

    def invalidate(self, key, reason="explicit"):
        path = _entry_path(self.memory_dir, key)
        removed = os.path.exists(path)
        if removed:
            os.remove(path)
        self._telemetry(key, "invalidated", None, invalidation_reason=reason)
        return removed

    def invalidate_dependents(self, dependency_name, new_value, reason=None):
        """Dependency-aware invalidation (item C): only entries that
        actually recorded `dependency_name` are removed, and only when
        their recorded value differs from `new_value` -- never a
        blanket clear of the whole store."""
        removed = []
        for key, entry in list(self._all_entries()):
            recorded = (entry.get("dependencies") or {}).get(dependency_name)
            if recorded is not None and recorded != new_value:
                self.invalidate(key, reason=reason or
                                ("%s changed" % dependency_name))
                removed.append(key)
        return removed

    def prune_expired(self):
        removed = []
        for key, entry in list(self._all_entries()):
            if self._is_expired(entry):
                self.invalidate(key, reason="ttl_expired")
                removed.append(key)
        return removed

    def clear(self, category=None):
        removed = []
        for key, entry in list(self._all_entries()):
            if category is None or entry.get("category") == category:
                self.invalidate(key, reason="explicit_clear")
                removed.append(key)
        return removed

    # -- provider-native telemetry (item F) ------------------------------------

    def record_provider_cache_observation(self, key, backend_type, usage):
        """Honest labelling: CLI/subscription backends never claim
        provider caching (nothing exposes it to us), so they are always
        `provider_cache_unknown`. Every other backend reports based on
        whatever the ACTUAL response usage carried -- never invented."""
        status, cached_tokens = classify_provider_cache_report(
            backend_type, usage)
        self._telemetry(key, "provider_cache_observation", None,
                        provider_cache={"status": status,
                                       "cached_tokens": cached_tokens})
        return status, cached_tokens

    # -- introspection ----------------------------------------------------------

    def inspect(self, key):
        entry = self._read(key)
        if entry is None:
            return None
        out = dict(entry)
        path = _entry_path(self.memory_dir, key)
        out["storage_bytes"] = os.path.getsize(path) \
            if os.path.exists(path) else 0
        try:
            created = _dt.datetime.fromisoformat(entry["created_at"])
            out["age_seconds"] = int(
                (self.clock() - created).total_seconds())
        except (KeyError, ValueError):
            out["age_seconds"] = None
        return out

    def status(self):
        entries = list(self._all_entries())
        by_category = {}
        total_bytes = 0
        for key, entry in entries:
            cat = entry.get("category", "unknown")
            by_category[cat] = by_category.get(cat, 0) + 1
            path = _entry_path(self.memory_dir, key)
            if os.path.exists(path):
                total_bytes += os.path.getsize(path)
        telemetry = self._read_telemetry()
        hits = sum(1 for t in telemetry
                  if t.get("event") == "application_cache_hit")
        misses = sum(1 for t in telemetry
                    if t.get("event") == "application_cache_miss")
        total = hits + misses
        provider_obs = [t for t in telemetry
                        if t.get("event") == "provider_cache_observation"]
        provider_reported = sum(
            1 for t in provider_obs
            if (t.get("provider_cache") or {}).get("status")
            == "provider_cache_reported")
        return {
            "entries": len(entries), "by_category": by_category,
            "storage_bytes": total_bytes,
            "application_cache_hits": hits,
            "application_cache_misses": misses,
            "hit_rate": round(hits / total, 3) if total else None,
            "tokens_avoided_estimated": sum(
                t.get("tokens_estimated") or 0 for t in telemetry
                if t.get("event") == "application_cache_hit"),
            "provider_cache_observations": len(provider_obs),
            "provider_cache_reported_count": provider_reported,
            "invalidations": sum(1 for t in telemetry
                                 if t.get("event") == "invalidated"),
        }

    def verify(self):
        """Cheap integrity check: nothing in the store looks sensitive
        (defence in depth -- content is also checked at write time;
        this catches anything that slipped in before a safety-rule
        update) and every entry is a recognised category."""
        problems = []
        for key, entry in self._all_entries():
            text_repr = entry.get("value") if isinstance(
                entry.get("value"), str) else _canonical(entry.get("value"))
            if _is_sensitive(entry.get("category"), text_repr):
                problems.append({"key": key, "problem": "sensitive content"})
            if entry.get("category") not in _ALL_ALLOWED_CATEGORIES:
                problems.append({"key": key, "problem": "unknown category"})
        checked = len(list(self._all_entries()))
        return {"ok": not problems, "problems": problems,
               "entries_checked": checked}

    def explain(self, run_id):
        """Every cache-related telemetry event whose key was looked up
        during a specific run -- best-effort correlation by key prefix
        embedded by the caller (callers should include the run_id as
        part of the components passed to compute_cache_key when they
        want this to resolve precisely)."""
        return [t for t in self._read_telemetry()
               if run_id and run_id in (t.get("key") or "")]

    # -- internals ----------------------------------------------------------------

    def _read_raw(self, key):
        """Load the entry exactly as stored, with NO ttl side effect --
        used by bulk/maintenance operations (`_all_entries`) so they see
        the true current state and can account for what they themselves
        remove, rather than having entries silently vanish as a side
        effect of iteration."""
        path = _entry_path(self.memory_dir, key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _is_expired(self, entry):
        ttl = entry.get("ttl_seconds")
        if ttl is None:
            return False
        try:
            created = _dt.datetime.fromisoformat(entry["created_at"])
        except (KeyError, ValueError):
            return False
        return (self.clock() - created).total_seconds() > ttl

    def _read(self, key):
        """Single-key lookup used by `get()`: lazily expires-and-removes
        on access (the normal, expected cache behaviour for a read)."""
        entry = self._read_raw(key)
        if entry is None:
            return None
        if self._is_expired(entry):
            self.invalidate(key, reason="ttl_expired")
            return None
        return entry

    def _write(self, key, entry):
        os.makedirs(_cache_dir(self.memory_dir), exist_ok=True)
        path = _entry_path(self.memory_dir, key)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entry, fh, indent=2, default=str)
        os.replace(tmp, path)

    def _all_entries(self):
        """Bulk iteration over every entry AS STORED -- expired entries
        ARE included (callers that care, like `prune_expired`, decide
        what to do with them); this never silently removes anything as
        a side effect of iterating."""
        cache_dir = _cache_dir(self.memory_dir)
        if not os.path.isdir(cache_dir):
            return
        for name in sorted(os.listdir(cache_dir)):
            if not name.endswith(".json") or name.endswith(".tmp"):
                continue
            key = name[:-5]
            entry = self._read_raw(key)
            if entry is not None:
                yield key, entry

    def _telemetry(self, key, event, category, tokens_estimated=None,
                   invalidation_reason=None, provider_cache=None):
        os.makedirs(self.memory_dir, exist_ok=True)
        record = {"ts": self.clock().isoformat(timespec="seconds"),
                  "key": key, "event": event, "category": category,
                  "tokens_estimated": tokens_estimated,
                  "invalidation_reason": invalidation_reason}
        if provider_cache is not None:
            record["provider_cache"] = provider_cache
        path = os.path.join(self.memory_dir, TELEMETRY_FILENAME)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def _read_telemetry(self, limit=5000):
        path = os.path.join(self.memory_dir, TELEMETRY_FILENAME)
        if not os.path.exists(path):
            return []
        out = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out[-limit:]


def classify_provider_cache_report(backend_type, usage):
    """CLI/subscription backends never claim provider caching -- nothing
    exposes it to us over that interface, so honesty requires
    `provider_cache_unknown` unconditionally. Every other backend type
    reports `provider_cache_reported` ONLY when the actual response
    usage carried a cached-token field (never invented, never
    estimated -- that's the application cache's job, kept in a
    separate, clearly-labelled counter)."""
    usage = usage or {}
    if backend_type == "cli":
        return "provider_cache_unknown", None
    cached = usage.get("cached_tokens")
    if cached is None:
        cached = usage.get("cached_input_tokens")
    if cached is not None:
        return "provider_cache_reported", int(cached)
    return "provider_cache_unknown", None
