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


def load_config(path=None, env=None):
    env = env if env is not None else os.environ
    path = Path(path or env.get("AGENTIC_CONFIG") or (AGENTIC_DIR / "config.yaml"))
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg = copy.deepcopy(cfg)
    _apply_env_overrides(cfg, env)
    if cfg.get("project", {}).get("name") in (None, "auto"):
        cfg.setdefault("project", {})["name"] = repo_root(cfg).name
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
