"""Configuration loading with environment-variable overrides.

Override forms (checked in this order, all optional):
  AGENTIC_CONFIG=<path>                         alternative config file
  AGENTIC_<SECTION>_<KEY>=<value>               e.g. AGENTIC_EXECUTION_MODE=auto,
      AGENTIC_BUDGET_DAILY_LIMIT_USD=10 (matched against existing keys of the
      section, longest key first, so multi-word keys work)
  AGENTIC_ROLE_<ROLE>_(MODEL|PROVIDER|MAX_OUTPUT_TOKENS|TEMPERATURE)=<value>
  AGENTIC_PROVIDER_<NAME>_(BASE_URL|API_KEY_ENV|TYPE)=<value>
"""
import copy
import os
from pathlib import Path

import yaml

AGENTIC_DIR = Path(__file__).resolve().parent.parent

_ROLE_FIELDS = ["MAX_OUTPUT_TOKENS", "TEMPERATURE", "PROVIDER", "MODEL"]
_PROVIDER_FIELDS = ["BASE_URL_ENV", "API_KEY_ENV", "BASE_URL", "TYPE"]
_SCALAR_SECTIONS = ["execution", "budget", "retry", "project"]


def _coerce(value):
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value


def repo_root(cfg):
    rel = str(cfg.get("project", {}).get("repository_root", ".."))
    return (AGENTIC_DIR / rel).resolve()


def deep_merge(base, override):
    """Recursively merge override into a copy of base (dicts only; other
    values are replaced)."""
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def set_path(cfg, dotted_key, value):
    """Set cfg['a']['b'] from 'a.b' (used by --set CLI overrides)."""
    node = cfg
    keys = dotted_key.split(".")
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = _coerce(value) if isinstance(value, str) else value


def load_config(path=None, env=None, profile=None, cli_overrides=None):
    """Configuration precedence (later wins):
    1. .agentic/config.yaml            (repository defaults, committed)
    2. .agentic/config.machine.yaml    (this computer; git-ignored; no secrets)
    3. .agentic/profiles/<name>.yaml   (selected named configuration)
    4. AGENTIC_* environment overrides
    5. CLI overrides (--primary/--fallback/--set)
    """
    env = env if env is not None else os.environ
    path = Path(path or env.get("AGENTIC_CONFIG") or (AGENTIC_DIR / "config.yaml"))
    cfg = _load_yaml(path)
    machine_path = AGENTIC_DIR / "config.machine.yaml"
    if machine_path.exists():
        cfg = deep_merge(cfg, _load_yaml(machine_path))
    profile = profile or env.get("AGENTIC_PROFILE")
    if profile:
        profile_path = AGENTIC_DIR / "profiles" / (profile + ".yaml")
        if not profile_path.exists():
            raise FileNotFoundError("profile %r not found at %s"
                                    % (profile, profile_path))
        cfg = deep_merge(cfg, _load_yaml(profile_path))
    cfg = copy.deepcopy(cfg)
    _apply_env_overrides(cfg, env)
    for override in (cli_overrides or {}).items():
        set_path(cfg, override[0], override[1])
    if cfg.get("project", {}).get("name") in (None, "auto"):
        cfg.setdefault("project", {})["name"] = repo_root(cfg).name
    from .migrate import migrate
    cfg["_migration"] = migrate(cfg)
    return cfg


def _match_key(section, upper_key):
    """Map DAILY_LIMIT_USD to the existing YAML key daily_limit_usd."""
    for key in sorted(section.keys(), key=len, reverse=True):
        if key.upper() == upper_key:
            return key
    return upper_key.lower()


def _apply_env_overrides(cfg, env):
    for name, raw in env.items():
        if not name.startswith("AGENTIC_"):
            continue
        rest = name[len("AGENTIC_"):]
        if rest.startswith("ROLE_"):
            _apply_suffixed(cfg.setdefault("roles", {}), rest[5:], _ROLE_FIELDS, raw)
        elif rest.startswith("PROVIDER_"):
            _apply_suffixed(cfg.setdefault("providers", {}), rest[9:], _PROVIDER_FIELDS, raw)
        else:
            for section_name in _SCALAR_SECTIONS:
                prefix = section_name.upper() + "_"
                if rest.startswith(prefix):
                    section = cfg.setdefault(section_name, {})
                    key = _match_key(section, rest[len(prefix):])
                    section[key] = _coerce(raw)
                    break


def _apply_suffixed(table, rest, fields, raw):
    for field in fields:
        suffix = "_" + field
        if rest.endswith(suffix):
            entry_name = rest[: -len(suffix)].lower()
            entry = table.setdefault(entry_name, {})
            if isinstance(entry, dict):
                entry[field.lower()] = _coerce(raw)
            return


def resolve_role(cfg, role):
    roles = cfg.get("roles", {})
    if role not in roles:
        raise KeyError("role %r is not configured" % role)
    return roles[role]


def provider_config(cfg, name):
    providers = cfg.get("providers", {})
    if name not in providers:
        raise KeyError("provider %r is not configured" % name)
    return providers[name]
