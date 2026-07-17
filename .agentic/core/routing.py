"""Capability-based model routing (ADR 0005).

`routing.mode: capability` selects backends by declared capability,
machine availability, breaker state, and historical success — never by
assuming what models exist on a machine. The preserved `simple` and
`per_agent` modes are untouched.

Enforced policies:
- backends in authentication_required breaker state are excluded (an auth
  failure is never routed around);
- embedding models are never selected for generative roles;
- reviewer roles prefer a different backend than the worker
  (`reviewer_different_from_worker`); when only the worker's backend is
  usable the decision records the violation instead of silently passing;
- `allow_local_fallback: false` keeps local backends out of fallbacks;
- every chain computation persists an explanation record
  (.agentic/memory/routing-decisions.jsonl).
"""
import datetime as _dt
import json
import os
import re

from .redact import redact

LEVELS = {"none": 0, "low": 1, "medium": 2, "high": 3, "highest": 4}

# roles whose output is prose/code generation: embedding models excluded
EMBEDDING_RE = re.compile(r"(?i)(embed|embedding|bge-|-e5|e5-|minilm|"
                          r"nomic-embed)")

REVIEWER_ROLES = {"qa", "qa_reviewer", "security", "security_reviewer",
                  "verifier", "final_auditor"}
ROLE_ALIASES = {"qa_reviewer": "qa", "security_reviewer": "security",
                "worker": "coder", "memory_summarizer": "conductor"}

# honest defaults by backend type; overridable per backend in config
TYPE_CAPABILITIES = {
    "cli": {"reasoning": "high", "coding": "high", "review": "high",
            "long_running": True},
    "api": {"reasoning": "high", "coding": "high", "review": "high",
            "long_running": True},
    "local": {"reasoning": "medium", "coding": "medium", "review": "medium",
              "long_running": True},
    "custom_command": {"reasoning": "medium", "coding": "medium",
                       "review": "medium", "long_running": True},
}

DECISIONS_FILE = "routing-decisions.jsonl"


def routing_config(cfg):
    return cfg.get("routing") or {}


def policies(cfg):
    merged = {"reviewer_different_from_worker": False,
              "no_fallback_on_auth_failure": True,
              "no_fallback_on_refusal": True,
              "allow_local_fallback": True}
    merged.update(routing_config(cfg).get("policies") or {})
    return merged


def backend_capabilities(bcfg):
    caps = dict(TYPE_CAPABILITIES.get(bcfg.get("type", "api"),
                                      TYPE_CAPABILITIES["api"]))
    caps.update(bcfg.get("capabilities") or {})
    return caps


def _level(value):
    if isinstance(value, bool):
        return LEVELS["high"] if value else LEVELS["none"]
    return LEVELS.get(str(value).lower(), LEVELS["medium"])


def _satisfies(caps, required):
    """(ok, reason). `highest` requirements are handled by ordering, so
    they filter nothing here."""
    for name, need in (required or {}).items():
        need_level = _level(need)
        if need_level >= LEVELS["highest"]:
            continue
        have = _level(caps.get(name, "none" if isinstance(need, str)
                               else False))
        if have < need_level:
            return False, "capability %s=%s below required %s" \
                % (name, caps.get(name), need)
    return True, ""


def _success_rate(ledger, backend):
    if ledger is None:
        return None
    try:
        calls = ledger.calls_in_window(backend, 24)
    except Exception:
        return None
    if not calls:
        return None
    ok = sum(1 for c in calls if str(c.get("ok")) in ("1", "True"))
    return round(ok / len(calls), 3)


def agent_spec(cfg, role):
    agents = routing_config(cfg).get("agents") or {}
    spec = agents.get(role)
    if spec is None:
        alias = ROLE_ALIASES.get(role)
        reverse = {v: k for k, v in ROLE_ALIASES.items()}
        spec = agents.get(alias) or agents.get(reverse.get(role)) or {}
    return spec or {}


def capability_chain(cfg, role, memory_dir=None, board=None, ledger=None,
                     worker_chain=None, clock=None):
    """Ordered backend chain for `role` under capability routing, with a
    persisted decision record. Returns [] when nothing is usable."""
    spec = agent_spec(cfg, role)
    required = spec.get("capabilities") or {}
    pol = policies(cfg)
    backends_cfg = cfg.get("backends") or {}
    preferred = [p.get("backend") for p in (spec.get("preferred") or [])
                 if p.get("backend")]

    candidates, rejected = [], []
    for name, bcfg in backends_cfg.items():
        caps = backend_capabilities(bcfg or {})
        model = (bcfg or {}).get("model")
        if model and EMBEDDING_RE.search(str(model)):
            rejected.append({"backend": name,
                             "reason": "embedding model %r cannot serve "
                                       "generative roles" % model})
            continue
        state = board.state(name) if board else "available"
        if state == "authentication_required":
            rejected.append({"backend": name,
                             "reason": "authentication required — never "
                                       "routed around"})
            continue
        ok, why = _satisfies(caps, required)
        if not ok and name not in preferred:
            # an explicitly-preferred backend is admin intent and stays in
            # the chain (as a fallback) even below the capability bar
            rejected.append({"backend": name, "reason": why})
            continue
        cap_strength = sum(_level(v) for v in caps.values()
                           if not isinstance(v, bool))
        candidates.append({
            "backend": name, "type": (bcfg or {}).get("type", "api"),
            "strength": cap_strength,
            "breaker_state": state,
            "success_rate": _success_rate(ledger, name),
            "preferred_rank": preferred.index(name)
            if name in preferred else len(preferred) + 1})

    def sort_key(c):
        degraded = 0 if c["breaker_state"] in ("available", "degraded") \
            else 1
        rate = c["success_rate"] if c["success_rate"] is not None else 0.5
        return (degraded, c["preferred_rank"], -c["strength"], -rate,
                c["backend"])

    candidates.sort(key=sort_key)
    chain = [c["backend"] for c in candidates]

    warnings = []
    independent_from = spec.get("independent_from")
    wants_independence = (pol.get("reviewer_different_from_worker")
                          and role in REVIEWER_ROLES) or independent_from
    if wants_independence and worker_chain:
        worker_primary = worker_chain[0]
        different = [b for b in chain if b != worker_primary]
        if different:
            chain = different + [b for b in chain if b == worker_primary]
        elif chain:
            warnings.append("reviewer independence unsatisfiable: only %r "
                            "is usable" % worker_primary)

    if not pol.get("allow_local_fallback", True):
        chain = chain[:1] + [b for b in chain[1:]
                             if backends_cfg.get(b, {}).get("type")
                             != "local"]

    _record_decision(memory_dir, {
        "role": role, "mode": "capability", "chain": chain,
        "required_capabilities": required, "rejected": rejected,
        "candidates": candidates, "warnings": warnings,
        "policies": {k: pol[k] for k in
                     ("reviewer_different_from_worker",
                      "allow_local_fallback")}}, clock)
    return chain


def _record_decision(memory_dir, record, clock=None):
    if not memory_dir:
        return
    record = dict(record, at=(clock or _dt.datetime.now)()
                  .isoformat(timespec="seconds"))
    try:
        os.makedirs(memory_dir, exist_ok=True)
        path = os.path.join(memory_dir, DECISIONS_FILE)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(redact(json.dumps(record, default=str)) + "\n")
    except OSError:
        pass


def read_decisions(memory_dir, limit=20):
    path = os.path.join(memory_dir, DECISIONS_FILE)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out[-limit:]


# -- discovery (probing; used by CLI/doctor/dashboard, not per-call) ---------------

def discover(cfg, memory_dir, runner=None, transport=None, which=None,
             env=None, board=None, ledger=None):
    """Full provider discovery with live probes. Reports are labelled:
    installed/auth come from the adapters, success rates are local history,
    capacity is always estimated/unknown — never invented."""
    from . import backends as backends_mod
    from .breaker import BreakerBoard
    from .capacity import CapacityLedger
    board = board or BreakerBoard(memory_dir)
    ledger = ledger or CapacityLedger(cfg, memory_dir)
    out = []
    for name, bcfg in (cfg.get("backends") or {}).items():
        entry = {"backend": name, "type": (bcfg or {}).get("type", "api"),
                 "capabilities": backend_capabilities(bcfg or {}),
                 "configured_model": (bcfg or {}).get("model"),
                 "context_window": (bcfg or {}).get("context_window"),
                 "breaker_state": board.state(name),
                 "success_rate_24h": _success_rate(ledger, name),
                 "capacity_confidence": "unknown"}
        try:
            adapter = backends_mod.build_backend(
                cfg, name, runner=runner, transport=transport, which=which,
                env=env)
            detection = adapter.detect()
            entry["installed"] = detection.get("installed", False)
            entry["version"] = detection.get("version")
            entry["models"] = detection.get("models", [])
            entry["auth"] = adapter.auth_status()
            caps = getattr(adapter, "capabilities", {}) or {}
            entry["structured_output"] = caps.get("structured_output")
            entry["tool_support"] = caps.get("tool_calling")
        except Exception as exc:   # noqa: BLE001
            entry["installed"] = False
            entry["error"] = str(exc)[:200]
        out.append(entry)
    return out
