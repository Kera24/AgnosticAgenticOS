"""Model Capability Registry (Phase 8): a dynamic, per-machine catalogue
of what models are ACTUALLY available right now, classified into
capability tiers -- never a hardcoded list of specific model names.
Configured model names/aliases are preferences the registry tries to
honour; nothing here assumes any particular model (Opus, Sonnet, GPT-*,
Qwen-*, or anything else) stays available forever -- classification is
driven by backend TYPE defaults plus admin-configured overrides/name
heuristics, all of which degrade to "unknown" rather than guessing.

Discovery reuses, never re-implements: `core.setupwiz.detect_backends`
(installed/auth/local-model probing), `core.authx.backend_auth_report`
(accurate auth + smoke-test state), `core.capacity.CapacityLedger`
(historical success/failure), `core.breaker.BreakerBoard` (circuit-
breaker state), and `core.modelres` (the actual model-string resolution
used at invocation time -- this registry never invents a second answer
to "what model string gets sent").
"""
import datetime as _dt
import json
import os
import re

CAPABILITY_CLASSES = ("frontier", "high", "medium", "lightweight", "unknown")
_CLASS_RANK = {c: i for i, c in enumerate(CAPABILITY_CLASSES)}   # 0 = best

# Backend TYPE defaults -- subscription CLIs are treated as this
# platform's strongest tier by design (that's the whole point of routing
# orchestration through them); local models default to medium pending
# a name-based refinement; API/custom_command models carry no reliable
# signal at all without configuration, so they start "unknown" rather
# than guessed.
DEFAULT_BACKEND_TYPE_CLASS = {"cli": "frontier", "local": "medium",
                              "api": "unknown", "custom_command": "unknown"}

# Secondary, low-confidence refinement -- only ever used to move a model
# OUT of "unknown", never to override an explicit config rule.
_LIGHTWEIGHT_HINTS = re.compile(
    r"(?i)(mini|haiku|lite|flash|nano|\bsmall\b|\b[1-3]b\b)")
_HIGH_HINTS = re.compile(
    r"(?i)(opus|\blarge\b|\bpro\b|\b(70|72|405)b\b)")
_EMBEDDING_HINT = re.compile(r"(?i)embed")

ROLE_ALIAS_RE = re.compile(
    r"^(?P<role>[a-z][a-z0-9]*)_(?P<class>frontier|high|medium|lightweight)$")

REGISTRY_FILE = "model-registry.json"


class ModelCapError(Exception):
    pass


def _now():
    return _dt.datetime.now().isoformat(timespec="seconds")


def model_record(*, backend, provider, model_id, display_name=None,
                 discovered=True, authenticated=False, smoke_tested=False,
                 available=False, context_window=None,
                 reasoning_class="unknown", coding_class="unknown",
                 review_class="unknown", long_running=None,
                 structured_output=None, native_tools=None,
                 mcp_support=None, skills_support=None, subagents=None,
                 relative_cost=None, relative_speed=None, local=False,
                 historical_success=None, historical_failure=None,
                 capacity_status="unknown", capacity_confidence="unknown",
                 circuit_breaker="unknown", last_verified=None,
                 classification_reason=None):
    return {
        "backend": backend, "provider": provider, "model_id": model_id,
        "display_name": display_name or model_id or backend,
        "discovered": bool(discovered), "authenticated": bool(authenticated),
        "smoke_tested": bool(smoke_tested), "available": bool(available),
        "context_window": context_window,
        "reasoning_class": reasoning_class, "coding_class": coding_class,
        "review_class": review_class, "long_running": long_running,
        "structured_output": structured_output, "native_tools": native_tools,
        "mcp_support": mcp_support, "skills_support": skills_support,
        "subagents": subagents, "relative_cost": relative_cost,
        "relative_speed": relative_speed, "local": bool(local),
        "historical_success": historical_success,
        "historical_failure": historical_failure,
        "capacity_status": capacity_status,
        "capacity_confidence": capacity_confidence,
        "circuit_breaker": circuit_breaker, "last_verified": last_verified,
        "classification_reason": classification_reason,
    }


# -- classification (deterministic; never a model call) ------------------------------

def classify_model(backend_name, backend_type, model_id, *, overrides=None):
    """Returns (capability_class, reason). Checked in order: explicit
    config override (by backend and/or model_id pattern) -> backend-type
    default -> name-heuristic refinement of "unknown" -> "unknown"."""
    for rule in overrides or []:
        if rule.get("backend") not in (None, backend_name):
            continue
        pattern = rule.get("model_id_pattern")
        if pattern and model_id and not re.search(pattern, model_id,
                                                   re.IGNORECASE):
            continue
        cls = rule.get("class")
        if cls in CAPABILITY_CLASSES:
            return cls, "explicit override (%s)" % (pattern or backend_name)

    base = DEFAULT_BACKEND_TYPE_CLASS.get(backend_type, "unknown")
    if base != "unknown":
        return base, "backend type default (%s)" % backend_type
    if model_id:
        if _LIGHTWEIGHT_HINTS.search(model_id):
            return "lightweight", "name heuristic (lightweight keyword)"
        if _HIGH_HINTS.search(model_id):
            return "high", "name heuristic (high-tier keyword)"
    return "unknown", "no classification signal; configure " \
                      "model_capability.overrides for this backend/model"


def is_embedding_model_id(model_id):
    return bool(model_id) and bool(_EMBEDDING_HINT.search(model_id))


# -- the registry -------------------------------------------------------------------

class ModelCapabilityRegistry:
    def __init__(self, records=None, generated_at=None):
        self.records = list(records or [])
        self.generated_at = generated_at or _now()

    def by_backend(self, backend):
        return [r for r in self.records if r["backend"] == backend]

    def by_class(self, capability_class, *, available_only=True):
        return [r for r in self.records
               if r["reasoning_class"] == capability_class
               and (not available_only or r["available"])]

    def best(self, capability_class, *, prefer_backend=None, local=None):
        """Best available record at `capability_class`, or the next
        lower tier if nothing is available there (graceful degradation
        -- Phase 9 decides whether that's acceptable for a given role).
        Returns None only when NOTHING is available at all."""
        start = _CLASS_RANK.get(capability_class, len(CAPABILITY_CLASSES) - 1)
        for rank in range(start, len(CAPABILITY_CLASSES)):
            cls = CAPABILITY_CLASSES[rank]
            candidates = [r for r in self.records if r["available"]
                         and r["reasoning_class"] == cls
                         and (local is None or r["local"] == local)]
            if not candidates:
                continue
            candidates.sort(key=lambda r: (
                r["backend"] != prefer_backend, r["circuit_breaker"] not in
                ("available", "degraded"),
                -(r["historical_success"] or 0), r["backend"],
                r["model_id"] or ""))
            return candidates[0]
        return None

    def resolve_alias(self, alias, *, prefer_backend=None):
        """'orchestrator_frontier' -> best available frontier-class
        record; 'local_fallback' -> best available local record,
        regardless of class. Raises only for a genuinely malformed alias
        -- "nothing available" returns None, it's a routing decision,
        not a programming error."""
        if alias == "local_fallback":
            return self.best("lightweight", local=True) or \
                self.best("medium", local=True) or \
                self.best("frontier", local=True)
        m = ROLE_ALIAS_RE.match(alias)
        if not m:
            raise ModelCapError(
                "unrecognised model alias %r (expected "
                "<role>_<frontier|high|medium|lightweight>, or "
                "'local_fallback')" % alias)
        return self.best(m.group("class"), prefer_backend=prefer_backend)

    def to_dict(self):
        return {"generated_at": self.generated_at, "records": self.records}

    @classmethod
    def from_dict(cls, data):
        return cls(data.get("records") or [], data.get("generated_at"))


# -- discovery: assemble ModelRecords from the EXISTING detection layers -----------

def discover_registry(cfg, *, memory_dir=None, runner=None, which=None,
                      transport=None, env=None, detected=None, apis=None,
                      auth_reports=None):
    """Live discovery -- makes the same safe version/auth/model-list
    probes `setup`/`doctor` already make (never a generation call).
    Every field the registry can't honestly determine stays None/
    "unknown" rather than being guessed.

    `detected`/`apis`/`auth_reports`: pass already-computed results
    (e.g. from `doctor`, which probes these anyway) to avoid a second,
    redundant round of live probing."""
    from .breaker import BreakerBoard
    from .capacity import CapacityLedger

    if detected is None or apis is None:
        from .setupwiz import detect_backends
        detected, apis = detect_backends(cfg, runner=runner, which=which,
                                         transport=transport)
    if auth_reports is None:
        auth_reports = {}
        if memory_dir:
            from .authx import backend_auth_report
            auth_reports = backend_auth_report(cfg, memory_dir,
                                               runner=runner, which=which,
                                               env=env)
    board = BreakerBoard(memory_dir) if memory_dir else None
    ledger = CapacityLedger(cfg, memory_dir) if memory_dir else None
    overrides = ((cfg.get("model_capability") or {}).get("overrides")) or []
    backends_cfg = cfg.get("backends") or {}

    records = []
    for name, info in detected.items():
        btype = (backends_cfg.get(name) or {}).get(
            "type", "local" if name == "ollama" else "cli")
        auth = auth_reports.get(name) or {}
        auth_ok = auth.get("state") in ("authenticated", "local_ok")
        smoke = (auth.get("smoke_test") or {}).get("ok")
        circuit = board.state(name) if board else "unknown"
        success_rate = None
        if ledger:
            try:
                calls = ledger.calls_in_window(name, 24)
                if calls:
                    ok = sum(1 for c in calls
                            if str(c.get("ok")) in ("1", "True"))
                    success_rate = round(ok / len(calls), 3)
            except Exception:
                success_rate = None

        model_ids = info.get("models") if btype == "local" else \
            [(backends_cfg.get(name) or {}).get("model")]
        for model_id in (model_ids or [None]):
            if is_embedding_model_id(model_id):
                continue   # never surfaced as a generative capability
            cap_class, reason = classify_model(name, btype, model_id,
                                               overrides=overrides)
            records.append(model_record(
                backend=name, provider=name,
                model_id=model_id or "provider_default",
                display_name=info.get("version") or name,
                discovered=True, authenticated=auth_ok,
                smoke_tested=bool(smoke),
                available=bool(info.get("installed")) and auth_ok,
                reasoning_class=cap_class, coding_class=cap_class,
                review_class=cap_class, local=(btype == "local"),
                historical_success=success_rate,
                capacity_confidence="estimated" if success_rate is not None
                else "unknown",
                circuit_breaker=circuit, last_verified=_now(),
                classification_reason=reason))

    for pname, info in apis.items():
        btype = "api"
        model_id = ((backends_cfg.get(pname) or {}).get("model"))
        cap_class, reason = classify_model(pname, btype, model_id,
                                           overrides=overrides)
        records.append(model_record(
            backend=pname, provider=pname, model_id=model_id,
            display_name=pname, discovered=True,
            authenticated=bool(info.get("configured")),
            smoke_tested=False,
            available=bool(info.get("configured")) and bool(model_id),
            reasoning_class=cap_class, coding_class=cap_class,
            review_class=cap_class, local=False,
            circuit_breaker=board.state(pname) if board else "unknown",
            last_verified=_now(), classification_reason=reason))

    return ModelCapabilityRegistry(records)


# -- persistence (machine-local; this is a snapshot, always re-derivable) -----------

def _registry_path(memory_dir):
    return os.path.join(str(memory_dir), REGISTRY_FILE)


def save_registry(memory_dir, registry):
    path = _registry_path(memory_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(registry.to_dict(), fh, indent=2, default=str)
    os.replace(tmp, path)
    return path


def load_registry(memory_dir):
    path = _registry_path(memory_dir)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return ModelCapabilityRegistry.from_dict(json.load(fh))
