"""Safe, deterministic probes for promotion-gated features.

Probes replay real persisted project evidence through production policy code.
They never edit project state, run a model, or count as build successes;
distinct passing probes are tracked separately as stable-promotion evidence.
"""
import copy
import datetime as _dt
import json
import os
import tempfile

from . import contract
from .featuregates import FeatureGateRegistry


def _read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _latest_amendment_run(runs_dir):
    try:
        names = sorted(
            (name for name in os.listdir(runs_dir)
             if name.startswith("cycle-")), reverse=True)
    except OSError:
        names = []
    for name in names:
        run_dir = os.path.join(runs_dir, name)
        ledger_path = os.path.join(run_dir, "contract-amendments.json")
        contract_path = os.path.join(run_dir, "task-contract.json")
        try:
            ledger = _read_json(ledger_path)
            task_contract = _read_json(contract_path)
        except (OSError, ValueError):
            continue
        if ledger.get("proposals") and \
                (ledger.get("policy") or {}).get("enabled") is True:
            return name.replace("cycle-", "", 1), ledger, task_contract
    raise ValueError("no persisted contract-amendment proposal available")


def _task_from_evidence(ledger, task_contract):
    return {
        "id": task_contract.get("task_id"),
        "expected_paths": copy.deepcopy(
            task_contract.get("required_outputs") or []),
        "acceptance_criteria": list(
            task_contract.get("acceptance_criteria") or []),
        "deterministic_checks": list(
            task_contract.get("deterministic_checks") or []),
        "contract_amendment_policy": copy.deepcopy(
            ledger.get("policy") or {}),
    }


def _applied_values(order, decisions):
    applied = []
    for decision in decisions:
        if not decision.get("accepted"):
            continue
        kind = decision.get("kind")
        value = decision.get("normalized_value")
        if kind == "acceptance_criterion":
            present = value in (order.get("acceptance_criteria") or [])
        elif kind == "deterministic_check":
            present = value in (order.get("deterministic_checks") or [])
        elif kind == "allowed_path":
            present = value in (order.get("allowed_paths") or [])
        elif kind == "required_output":
            path = value.get("path") if isinstance(value, dict) else value
            present = path in (order.get("expected_outputs") or [])
        else:
            present = False
        applied.append({"id": decision.get("id"), "kind": kind,
                        "value": value, "applied": bool(present)})
    return applied


def _rollback_guard(memory_dir, cfg, project_id, feature):
    """Exercise live registry rollback code against an isolated copy."""
    with tempfile.TemporaryDirectory(prefix="agentic-feature-probe-") as temp:
        isolated = FeatureGateRegistry(temp, cfg)
        isolated.data = copy.deepcopy(
            FeatureGateRegistry(memory_dir, cfg).data)
        isolated._save()
        gate = isolated.decision(feature, project_id)
        run_id = "probe-rollback"
        isolated.begin_run(run_id, {feature: gate})
        events = isolated.record_outcome(
            run_id, "failure", platform_failure=True,
            detail="isolated canary rollback probe")
        after = isolated.decision(feature, project_id)
        return {
            "passed": bool(events and
                           events[0].get("action") ==
                           "rollback_canary_to_shadow" and
                           after.get("effective_state") == "shadow" and
                           not after.get("active")),
            "events": events,
            "resulting_state": after.get("effective_state"),
            "live_state_unchanged": True,
        }


def run_contract_amendment_probe(project_cfg, project_id):
    runtime = project_cfg.get("runtime") or {}
    base = runtime.get("project_dir")
    if not base:
        raise ValueError("feature probe requires a registered project")
    memory_dir = os.path.join(str(base), "memory")
    runs_dir = os.path.join(str(base), "runs")
    registry = FeatureGateRegistry(memory_dir, project_cfg)
    gate = registry.decision("contract_amendments", project_id)
    if not gate.get("active"):
        raise ValueError(
            "contract_amendments is not active for project %s" % project_id)

    source_run, ledger, task_contract = _latest_amendment_run(runs_dir)
    task = _task_from_evidence(ledger, task_contract)
    proposed = {
        "allowed_paths": list(task_contract.get("allowed_paths") or []),
        "contract_amendments": copy.deepcopy(ledger.get("proposals") or []),
    }
    compiled = contract.canonicalize_work_order(
        task, proposed, feature_gate=gate)
    decisions = compiled.get("contract_amendment_decisions") or []
    applied = _applied_values(compiled, decisions)
    accepted = [item for item in decisions if item.get("accepted")]
    replay_passed = bool(accepted and applied and
                         all(item["applied"] for item in applied))
    rollback = _rollback_guard(
        memory_dir, project_cfg, project_id, "contract_amendments")
    report = {
        "checked_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "project": project_id,
        "feature": "contract_amendments",
        "passed": bool(replay_passed and rollback["passed"]),
        "source_run": source_run,
        "feature_gate": gate,
        "accepted_count": len(accepted),
        "decisions": decisions,
        "applied": applied,
        "rollback_guard": rollback,
        "mutated_project_state": False,
    }
    report_dir = os.path.join(memory_dir, "feature-probes")
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(
        report_dir, "contract-amendments-%s.json" %
        _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    report["report_path"] = path
    report["evidence_recorded"] = registry.record_probe(
        "contract_amendments", project_id, source_run, report["passed"],
        report_path=path,
        detail="active contract-amendment replay and rollback guard")
    report["feature_status"] = registry.status(
        "contract_amendments", project_id=project_id)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    return report
