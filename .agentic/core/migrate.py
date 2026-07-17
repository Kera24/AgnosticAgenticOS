"""Versioned configuration migration.

Config version 1 (pre-upgrade) files keep working unchanged: every new
subsystem reads its section through a defaulting accessor. migrate()
normalises a loaded config to the CURRENT_VERSION shape in memory — it
NEVER rewrites the user's file — and records which sections were filled so
doctor can report the effective version honestly.
"""
CURRENT_VERSION = 2

# sections introduced by version 2 and their minimal defaults
V2_SECTIONS = {
    "context": {"enabled": True},
    "memory": {"enabled": True},
    "knowledge": {"enabled": True},
    "skills": {"enabled": True, "auto_install": False},
    "caching": {"enabled": True},
}


def migrate(cfg):
    """Normalise cfg in place; returns a report dict."""
    original = int(cfg.get("version") or 1)
    filled = []
    for section, defaults in V2_SECTIONS.items():
        if section not in cfg or cfg[section] is None:
            cfg[section] = dict(defaults)
            filled.append(section)
    routing = cfg.setdefault("routing", {})
    routing.setdefault("mode", "simple")
    routing.setdefault("policies", {})
    routing.setdefault("agents", {})
    cfg["version"] = CURRENT_VERSION
    return {"from_version": original, "to_version": CURRENT_VERSION,
            "sections_filled": filled,
            "migrated": original < CURRENT_VERSION or bool(filled)}
