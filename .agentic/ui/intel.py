"""Dashboard snapshots for the Phase 1–6 subsystems: context intelligence,
memory, knowledge vault, skills, and routing. Read paths only assemble
persisted state; the single mutation (memory forget, skill enable/disable)
is confirmed, audited, and validated — the dashboard never bypasses the
CLI's policy layer."""
import os

from core import config as config_mod
from core.codeintel import ci_config, get_adapter
from core.context.ledger import read_packages
from core.knowledge import KnowledgeVault
from core.memsvc import get_memory, memory_config
from core.routing import policies, read_decisions, routing_config
from core.skillreg import SkillRegistry


def _memory_dir():
    return str(config_mod.AGENTIC_DIR / "memory")


def context_snapshot(cfg, package_limit=25):
    root = str(config_mod.repo_root(cfg))
    adapter = get_adapter(cfg, root, _memory_dir())
    packages = read_packages(_memory_dir(), limit=package_limit)
    totals = {"estimated_savings_tokens": 0, "token_estimate": 0}
    for package in packages:
        totals["estimated_savings_tokens"] += \
            package.get("estimated_savings_tokens") or 0
        totals["token_estimate"] += package.get("token_estimate") or 0
    return {
        "code_intelligence": dict(
            adapter.status(),
            health=adapter.health_check(),
            configured_provider=ci_config(cfg)["provider"],
            fallback_reason=getattr(adapter, "fallback_reason", None)),
        "packages": list(reversed(packages)),
        "totals": dict(totals, measurement="estimated"),
    }


def context_search(cfg, query, limit=12):
    root = str(config_mod.repo_root(cfg))
    adapter = get_adapter(cfg, root, _memory_dir())
    return {"provider": adapter.provider_name,
            "results": adapter.search(query, limit=limit)}


def memory_snapshot(cfg):
    service = get_memory(cfg, _memory_dir())
    return dict(service.status(), config=memory_config(cfg))


def memory_search(cfg, query, limit=50, include_superseded=False):
    service = get_memory(cfg, _memory_dir())
    return {"records": service.search(query or None, limit=limit,
                                      include_superseded=include_superseded,
                                      include_sensitive=True)}


def memory_timeline(cfg, record_id):
    return {"timeline": get_memory(cfg, _memory_dir())
            .timeline(record_id)}


def memory_details(cfg, ids):
    return {"records": get_memory(cfg, _memory_dir()).details(ids)}


def memory_forget(cfg, record_id):
    return {"forgotten": get_memory(cfg, _memory_dir())
            .forget(record_id)}


def knowledge_snapshot(cfg):
    vault = KnowledgeVault(cfg, str(config_mod.AGENTIC_DIR))
    status = vault.status()
    docs = []
    for rel in vault.documents():
        doc = vault.read_doc(rel)
        meta = (doc or {}).get("meta") or {}
        docs.append({"path": rel, "id": meta.get("id"),
                     "type": meta.get("type"),
                     "updated": meta.get("updated"),
                     "conflict": rel.endswith(".incoming.md")})
    return dict(status, docs=docs)


def knowledge_document(cfg, rel):
    vault = KnowledgeVault(cfg, str(config_mod.AGENTIC_DIR))
    if not rel.endswith(".md"):
        raise ValueError("only markdown documents are served")
    doc = vault.read_doc(rel)          # vault.path() confines the path
    if doc is None:
        return None
    return {"path": rel, "meta": doc["meta"], "generated": doc["generated"],
            "user_section": doc["user_section"],
            "generated_intact": doc["generated_intact"]}


def skills_snapshot(cfg):
    registry = SkillRegistry(cfg, str(config_mod.AGENTIC_DIR))
    return {"skills": registry.list()}


def skill_action(cfg, skill_id, action):
    registry = SkillRegistry(cfg, str(config_mod.AGENTIC_DIR))
    if action == "enable":
        return registry.enable(skill_id)
    if action == "disable":
        return registry.disable(skill_id)
    if action == "verify":
        return registry.verify(skill_id)
    raise ValueError("unsupported skill action %r" % action)


def routing_snapshot(cfg, decision_limit=20):
    routing = routing_config(cfg)
    return {
        "mode": routing.get("mode", "simple"),
        "primary": routing.get("primary"),
        "fallbacks": routing.get("fallbacks") or [],
        "per_agent": routing.get("per_agent") or {},
        "agents": routing.get("agents") or {},
        "policies": policies(cfg),
        "decisions": list(reversed(read_decisions(_memory_dir(),
                                                  limit=decision_limit))),
    }
