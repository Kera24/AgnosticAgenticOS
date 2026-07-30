"""Persisted promotion gates for advanced Agentic OS capabilities.

States are monotonic by policy, not by model request:
disabled -> shadow -> canary -> stable. Runtime rollback is deliberately
one-way (canary -> shadow) after a platform failure. Promotion is an explicit
code/API action backed by accumulated evidence.
"""
import json
import os

STATES = ("disabled", "shadow", "canary", "stable")
DEFAULT_STATE = "disabled"
REGISTRY_FILE = "feature-gates.json"


class FeatureGateRegistry:
    def __init__(self, memory_dir, cfg=None):
        self.memory_dir = str(memory_dir)
        self.cfg = cfg or {}
        self.path = os.path.join(self.memory_dir, REGISTRY_FILE)
        self.data = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return {"version": 1, "features": {}, "runs": {}}
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {"version": 1, "features": {}, "runs": {}}
        data.setdefault("version", 1)
        data.setdefault("features", {})
        data.setdefault("runs", {})
        return data

    def _save(self):
        os.makedirs(self.memory_dir, exist_ok=True)
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
        os.replace(temp, self.path)

    def _config(self, name):
        return (((self.cfg.get("advanced_features") or {}).get(name))
                or {})

    def _record(self, name):
        record = self.data["features"].setdefault(name, {
            "successes": 0,
            "failures": 0,
            "platform_failures": 0,
            "rollback_count": 0,
            "override_state": None,
            "last_rollback_reason": None,
            "runtime_canary_projects": [],
        })
        record.setdefault("runtime_canary_projects", [])
        return record

    def decision(self, name, project_id=None):
        config = self._config(name)
        configured = str(config.get("state") or DEFAULT_STATE)
        if configured not in STATES:
            configured = DEFAULT_STATE
        record = self._record(name)
        state = record.get("override_state") or configured
        projects = sorted(set(config.get("canary_projects") or []) |
                          set(record.get("runtime_canary_projects") or []))
        canary_selected = (
            state != "canary" or
            bool(project_id and project_id in projects))
        active = state == "stable" or (state == "canary" and canary_selected)
        return {
            "feature": name,
            "configured_state": configured,
            "effective_state": state,
            "project_id": project_id,
            "canary_projects": projects,
            "canary_selected": canary_selected,
            "active": active,
            "observe_only": state == "shadow",
            "source": ("runtime_rollback" if record.get("override_state")
                       else "configuration"),
        }

    def begin_run(self, run_id, decisions):
        self.data["runs"][str(run_id)] = {
            "decisions": decisions,
            "outcome_recorded": False,
        }
        self._save()

    def record_outcome(self, run_id, outcome, platform_failure=False,
                       detail=None):
        run = self.data["runs"].get(str(run_id))
        if not run or run.get("outcome_recorded"):
            return []
        events = []
        for name, decision in (run.get("decisions") or {}).items():
            if not decision.get("active") and not decision.get("observe_only"):
                continue
            record = self._record(name)
            if outcome == "success":
                record["successes"] += 1
            else:
                record["failures"] += 1
            if platform_failure:
                record["platform_failures"] += 1
                if decision.get("effective_state") == "canary":
                    record["override_state"] = "shadow"
                    record["rollback_count"] += 1
                    record["last_rollback_reason"] = str(detail or outcome)[:300]
                    events.append({
                        "feature": name,
                        "action": "rollback_canary_to_shadow",
                        "reason": record["last_rollback_reason"],
                    })
        run.update({
            "outcome_recorded": True,
            "outcome": outcome,
            "platform_failure": bool(platform_failure),
        })
        self._save()
        return events

    def promote(self, name, target_state, minimum_successes=1,
                project_id=None):
        if target_state not in STATES:
            raise ValueError("unsupported feature state: %s" % target_state)
        record = self._record(name)
        if target_state == "canary" and not project_id:
            raise ValueError("canary promotion requires a project id")
        if target_state in ("canary", "stable") and \
                record.get("successes", 0) < int(minimum_successes):
            raise ValueError(
                "insufficient successful evidence for %s: %s < %s"
                % (name, record.get("successes", 0), minimum_successes))
        record["override_state"] = target_state
        if target_state == "canary" and project_id not in \
                record["runtime_canary_projects"]:
            record["runtime_canary_projects"].append(project_id)
        record["last_rollback_reason"] = None
        self._save()
        return self.decision(name, project_id=project_id)

    def status(self, name, project_id=None):
        return dict(
            self._record(name),
            **self.decision(name, project_id=project_id))
