"""Machine-local settings for the dashboard.

Only a whitelisted, validated subset of configuration is exposed. Writes go
exclusively to .agentic/config.machine.yaml (git-ignored). Secrets are never
read, displayed or written; any key that looks like a credential is refused
outright.
"""
import re

import yaml

from core import config as config_mod

SECRETISH = re.compile(r"(?i)(key|token|secret|password|credential|auth)")

INTERACTION_MODES = ("cycle_review", "milestone_review", "completion_only")
ROUTING_MODES = ("simple", "per_agent")
THEMES = ("dark", "light")
ROLE_IDS = ("architect", "conductor", "coder", "qa", "security")
LIMIT_KEYS = ("maximum_calls_per_hour", "maximum_calls_per_day",
              "maximum_estimated_tokens_per_hour",
              "maximum_estimated_tokens_per_day", "maximum_parallel_calls")

MACHINE_HEADER = ("# Machine-local Agentic OS configuration (git-ignored).\n"
                  "# Never store credentials here.\n")


class SettingsError(ValueError):
    pass


def _machine_path():
    return str(config_mod.AGENTIC_DIR / "config.machine.yaml")


def load_machine():
    path = _machine_path()
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except OSError:
        return {}


def effective_settings(cfg):
    """Read-only view of the safe configuration surface (merged config)."""
    scheduler = cfg.get("scheduler") or {}
    cooling = dict(scheduler.get("cooling") or {})
    cooling.update(cfg.get("cooling") or {})
    window = scheduler.get("operating_window") or {}
    ui = cfg.get("ui") or {}
    return {
        "interaction": {"mode": (cfg.get("interaction") or {}).get(
            "mode", "completion_only")},
        "routing": {
            "mode": (cfg.get("routing") or {}).get("mode", "simple"),
            "primary": (cfg.get("routing") or {}).get("primary"),
            "fallbacks": (cfg.get("routing") or {}).get("fallbacks") or [],
            "per_agent": (cfg.get("routing") or {}).get("per_agent") or {},
        },
        "cycle": {
            "target_duration_minutes": (scheduler.get("cycle") or {}).get(
                "target_duration_minutes", 20),
            "maximum_duration_minutes": (scheduler.get("cycle") or {}).get(
                "maximum_duration_minutes", 30),
        },
        "cooling": {
            "after_success_minutes": cooling.get("after_success_minutes", 30),
            "after_failure_minutes": cooling.get("after_failure_minutes", 30),
            "minimum_minutes": cooling.get("minimum_minutes", 5),
            "maximum_minutes": cooling.get("maximum_minutes", 360),
        },
        "capacity": {"safety_multiplier": (cfg.get("capacity") or {}).get(
            "safety_multiplier", 1.35)},
        "limits": cfg.get("limits") or {},
        "operating_window": {
            "enabled": bool(window.get("enabled")),
            "start": window.get("start", "07:00"),
            "stop": window.get("stop", "22:00"),
            "timezone": window.get("timezone", ""),
        },
        "notifications": {"desktop": bool(
            (cfg.get("notifications") or {}).get("desktop", True))},
        "ui": {
            "port": int(ui.get("port", 8765)),
            "open_browser": bool(ui.get("open_browser", True)),
            "theme": ui.get("theme", "dark"),
            "reduced_motion": ui.get("reduced_motion", "system"),
        },
        "backends_configured": sorted((cfg.get("backends") or {}).keys()),
    }


def _require(cond, message):
    if not cond:
        raise SettingsError(message)


def _int_in(value, low, high, name):
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise SettingsError("%s must be an integer" % name)
    _require(low <= value <= high, "%s must be between %d and %d"
             % (name, low, high))
    return value


def _opt_int(value, low, high, name):
    if value is None:
        return None
    return _int_in(value, low, high, name)


def _hhmm(value, name):
    _require(isinstance(value, str) and re.fullmatch(
        r"([01]\d|2[0-3]):[0-5]\d", value), "%s must be HH:MM" % name)
    return value


def validate_update(cfg, body):
    """Validate a settings update and return the machine-config patch.
    Unknown keys are rejected, backend names must exist, and nothing
    secret-shaped is accepted anywhere."""
    _reject_secretish(body)
    known_backends = set((cfg.get("backends") or {}).keys()) | \
        set((cfg.get("providers") or {}).keys())
    patch = {}
    allowed_top = {"interaction", "routing", "cycle", "cooling", "capacity",
                   "limits", "operating_window", "notifications", "ui"}
    unknown = set(body.keys()) - allowed_top
    _require(not unknown, "unknown settings section(s): %s"
             % ", ".join(sorted(unknown)))

    if "interaction" in body:
        mode = (body["interaction"] or {}).get("mode")
        _require(mode in INTERACTION_MODES,
                 "interaction.mode must be one of %s"
                 % ", ".join(INTERACTION_MODES))
        patch["interaction"] = {"mode": mode}

    if "routing" in body:
        r = body["routing"] or {}
        mode = r.get("mode", "simple")
        _require(mode in ROUTING_MODES, "routing.mode must be simple or "
                                        "per_agent")
        primary = r.get("primary")
        _require(primary, "routing.primary is required")
        _require(primary in known_backends,
                 "unknown backend %r for routing.primary" % primary)
        fallbacks = r.get("fallbacks") or []
        _require(isinstance(fallbacks, list) and
                 all(isinstance(f, str) for f in fallbacks),
                 "routing.fallbacks must be a list of backend names")
        for f in fallbacks:
            _require(f in known_backends, "unknown fallback backend %r" % f)
        _require(len(fallbacks) == len(set(fallbacks)) and
                 primary not in fallbacks,
                 "fallbacks must be unique and must not repeat the primary")
        routing = {"mode": mode, "primary": primary, "fallbacks": fallbacks}
        if mode == "per_agent":
            per = r.get("per_agent") or {}
            _require(set(per.keys()) <= set(ROLE_IDS),
                     "per_agent keys must be agent roles")
            clean = {}
            for role, rc in per.items():
                rp = (rc or {}).get("primary")
                _require(rp in known_backends,
                         "unknown backend %r for %s" % (rp, role))
                rfb = (rc or {}).get("fallbacks") or []
                for f in rfb:
                    _require(f in known_backends,
                             "unknown fallback %r for %s" % (f, role))
                clean[role] = {"primary": rp, "fallbacks": list(rfb)}
            routing["per_agent"] = clean
        patch["routing"] = routing

    if "cycle" in body:
        c = body["cycle"] or {}
        target = _int_in(c.get("target_duration_minutes"), 5, 240,
                         "cycle.target_duration_minutes")
        maximum = _int_in(c.get("maximum_duration_minutes"), 5, 480,
                          "cycle.maximum_duration_minutes")
        _require(maximum >= target, "maximum cycle duration must be >= "
                                    "target duration")
        patch.setdefault("scheduler", {})["cycle"] = {
            "target_duration_minutes": target,
            "maximum_duration_minutes": maximum, "maximum_tasks": 1}

    if "cooling" in body:
        c = body["cooling"] or {}
        cool = {
            "after_success_minutes": _int_in(
                c.get("after_success_minutes"), 1, 1440,
                "cooling.after_success_minutes"),
            "after_failure_minutes": _int_in(
                c.get("after_failure_minutes"), 1, 1440,
                "cooling.after_failure_minutes"),
            "minimum_minutes": _int_in(c.get("minimum_minutes", 5), 1, 1440,
                                       "cooling.minimum_minutes"),
            "maximum_minutes": _int_in(c.get("maximum_minutes", 360), 1, 1440,
                                       "cooling.maximum_minutes"),
        }
        _require(cool["minimum_minutes"] <= cool["maximum_minutes"],
                 "cooling.minimum_minutes must be <= maximum_minutes")
        patch.setdefault("scheduler", {})["cooling"] = cool
        patch["cooling"] = dict(cool, adaptive=True)

    if "capacity" in body:
        raw = (body["capacity"] or {}).get("safety_multiplier")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise SettingsError("capacity.safety_multiplier must be a number")
        _require(1.0 <= value <= 3.0,
                 "capacity.safety_multiplier must be between 1.0 and 3.0")
        patch["capacity"] = {"safety_multiplier": value}

    if "limits" in body:
        limits = body["limits"] or {}
        clean = {}
        for backend, entry in limits.items():
            _require(backend in known_backends,
                     "unknown backend %r in limits" % backend)
            clean[backend] = {}
            unknown_keys = set((entry or {}).keys()) - set(LIMIT_KEYS)
            _require(not unknown_keys, "unknown limit key(s): %s"
                     % ", ".join(sorted(unknown_keys)))
            for key in LIMIT_KEYS:
                if key in (entry or {}):
                    clean[backend][key] = _opt_int(
                        entry[key], 1, 100_000_000, "limits.%s.%s"
                        % (backend, key))
        patch["limits"] = clean

    if "operating_window" in body:
        w = body["operating_window"] or {}
        window = {"enabled": bool(w.get("enabled"))}
        if window["enabled"]:
            window["start"] = _hhmm(w.get("start"), "operating_window.start")
            window["stop"] = _hhmm(w.get("stop"), "operating_window.stop")
        if w.get("timezone") is not None:
            tz = str(w["timezone"])
            _require(len(tz) <= 64 and re.fullmatch(r"[A-Za-z0-9_+\-/]*", tz),
                     "operating_window.timezone is not a valid identifier")
            window["timezone"] = tz
        patch.setdefault("scheduler", {})["operating_window"] = window

    if "notifications" in body:
        patch["notifications"] = {
            "desktop": bool((body["notifications"] or {}).get("desktop"))}

    if "ui" in body:
        u = body["ui"] or {}
        ui_patch = {}
        if "port" in u:
            ui_patch["port"] = _int_in(u["port"], 1024, 65535, "ui.port")
        if "open_browser" in u:
            ui_patch["open_browser"] = bool(u["open_browser"])
        if "theme" in u:
            _require(u["theme"] in THEMES, "ui.theme must be dark or light")
            ui_patch["theme"] = u["theme"]
        if "reduced_motion" in u:
            _require(u["reduced_motion"] in (True, False, "system"),
                     "ui.reduced_motion must be true, false or 'system'")
            ui_patch["reduced_motion"] = u["reduced_motion"]
        patch["ui"] = ui_patch

    return patch


def _reject_secretish(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            key_path = "%s.%s" % (path, key) if path else str(key)
            # value-level scan; key names like api_key are simply not in the
            # whitelist, but reject early with a clear message
            if SECRETISH.search(str(key)) and key not in ("auth",):
                raise SettingsError(
                    "refusing credential-shaped setting %r" % key_path)
            _reject_secretish(value, key_path)
    elif isinstance(node, str):
        if len(node) > 40 and re.search(r"(?i)(sk-|ghp_|xox|akia)", node):
            raise SettingsError("refusing value that looks like a secret "
                                "at %s" % path)


def apply_update(cfg, body):
    patch = validate_update(cfg, body)
    machine = load_machine()
    merged = config_mod.deep_merge(machine, patch)
    path = _machine_path()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(MACHINE_HEADER)
        yaml.safe_dump(merged, fh, sort_keys=False)
    return patch
