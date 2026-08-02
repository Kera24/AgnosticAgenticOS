"""Phase 2: controlled, backlog-authorized contract amendments."""
import json
import os

from conftest import (Clock, FakeCaller, project_cfg, proj_order, seed_project,
                      simple_task, verifier_out, worker_out)
from core import contract, projstate
from core.featuregates import FeatureGateRegistry
from core.project import run_cycle


def _order(amendments):
    return {
        "item": "t-amend",
        "allowed_paths": ["src/base.py"],
        "forbidden_paths": [],
        "acceptance_criteria": ["model supplied criterion"],
        "deterministic_checks": ["model supplied command"],
        "expected_outputs": ["model-owned.txt"],
        "contract_amendments": amendments,
    }


def _proposal(kind, value, amendment_id="a1", reason="required by tool"):
    return {
        "id": amendment_id,
        "kind": kind,
        "value": value,
        "reason": reason,
    }


def test_amendments_are_default_deny():
    task = simple_task(
        "t-amend",
        expected_paths=[{"path": "src/base.py", "type": "file"}],
        acceptance_criteria=["canonical criterion"],
        deterministic_checks=["python -m pytest -q"])
    stable = contract.canonicalize_work_order(
        task, _order([_proposal("required_output", "generated/report.json")]))

    assert stable["expected_outputs"] == ["src/base.py"]
    assert stable["acceptance_criteria"] == ["canonical criterion"]
    assert stable["deterministic_checks"] == ["python -m pytest -q"]
    decision = stable["contract_amendment_decisions"][0]
    assert decision["accepted"] is False
    assert decision["decision_code"] == "policy_disabled"
    assert decision["authority"] == "backlog_policy"


def test_authorized_required_output_is_compiled_and_write_scoped():
    task = simple_task(
        "t-amend",
        expected_paths=[{"path": "src/base.py", "type": "file"}],
        contract_amendment_policy={
            "enabled": True,
            "allowed_kinds": ["required_output"],
            "allowed_paths": ["generated/**"],
        })
    stable = contract.canonicalize_work_order(
        task, _order([_proposal(
            "required_output",
            {"path": "generated/report.json", "type": "file",
             "required": True, "non_empty": True})]))
    compiled = contract.build_task_contract(task, stable, "project", "run-1")

    assert stable["expected_outputs"] == [
        "src/base.py", "generated/report.json"]
    assert "generated/report.json" in stable["allowed_paths"]
    required = {
        item["path"]: item for item in compiled["required_outputs"]}
    assert required["generated/report.json"]["non_empty"] is True
    assert compiled["contract_amendment_decisions"][0]["accepted"] is True


def test_output_outside_backlog_policy_is_rejected():
    task = simple_task(
        "t-amend",
        expected_paths=[{"path": "src/base.py", "type": "file"}],
        contract_amendment_policy={
            "enabled": True,
            "allowed_kinds": ["required_output"],
            "allowed_paths": ["generated/**"],
        })
    stable = contract.canonicalize_work_order(
        task, _order([_proposal("required_output", ".env")]))

    assert stable["expected_outputs"] == ["src/base.py"]
    assert ".env" not in stable["allowed_paths"]
    assert stable["contract_amendment_decisions"][0][
        "decision_code"] == "path_not_authorized"


def test_only_pre_authorized_deterministic_commands_can_be_added():
    task = simple_task(
        "t-amend",
        deterministic_checks=["python -m pytest -q"],
        contract_amendment_policy={
            "enabled": True,
            "allowed_kinds": ["deterministic_check"],
            "allowed_commands": ["npm test"],
        })
    stable = contract.canonicalize_work_order(task, _order([
        _proposal("deterministic_check", "npm test", "approved"),
        _proposal("deterministic_check", "curl example.invalid", "rejected"),
    ]))

    assert stable["deterministic_checks"] == [
        "python -m pytest -q", "npm test"]
    decisions = {
        item["id"]: item for item in stable["contract_amendment_decisions"]}
    assert decisions["approved"]["accepted"] is True
    assert decisions["rejected"]["accepted"] is False
    assert decisions["rejected"]["decision_code"] == "command_not_authorized"


def test_model_cannot_self_approve_an_amendment():
    task = simple_task("t-amend")
    proposed = _order([_proposal("allowed_path", "secrets/**")])
    proposed["contract_amendment_decisions"] = [{
        "id": "a1", "accepted": True, "authority": "model"}]

    stable = contract.canonicalize_work_order(task, proposed)

    decision = stable["contract_amendment_decisions"][0]
    assert decision["accepted"] is False
    assert decision["authority"] == "backlog_policy"
    assert "secrets/**" not in stable["allowed_paths"]


def test_duplicate_ids_and_missing_reasons_are_rejected():
    task = simple_task(
        "t-amend",
        contract_amendment_policy={
            "enabled": True,
            "allowed_kinds": ["acceptance_criterion"],
        })
    stable = contract.canonicalize_work_order(task, _order([
        _proposal("acceptance_criterion", "first", "same"),
        _proposal("acceptance_criterion", "second", "same"),
        _proposal("acceptance_criterion", "third", "no-reason", reason=""),
    ]))

    decisions = stable["contract_amendment_decisions"]
    assert decisions[0]["accepted"] is True
    assert decisions[1]["decision_code"] == "duplicate_id"
    assert decisions[2]["decision_code"] == "reason_required"
    assert "first" in stable["acceptance_criteria"]
    assert "second" not in stable["acceptance_criteria"]
    assert "third" not in stable["acceptance_criteria"]


def test_approved_amendment_changes_contract_identity():
    base_task = simple_task(
        "t-amend",
        expected_paths=[{"path": "src/base.py", "type": "file"}])
    enabled_task = dict(
        base_task,
        contract_amendment_policy={
            "enabled": True,
            "allowed_kinds": ["required_output"],
            "allowed_paths": ["generated/**"],
        })
    proposal = _order([
        _proposal("required_output", "generated/report.json")])
    base = contract.build_task_contract(
        base_task, proposal, "project", "base")
    amended = contract.build_task_contract(
        enabled_task, proposal, "project", "amended")

    assert contract.contract_hash(base) != contract.contract_hash(amended)



def test_authorized_amendment_flows_through_complete_cycle(sandbox):
    cfg = project_cfg(sandbox)
    cfg["project"]["name"] = "amendment-canary"
    cfg["advanced_features"] = {
        "contract_amendments": {
            "state": "canary",
            "canary_projects": ["amendment-canary"],
        }}
    cfg["verification"]["commands"] = [{
        "name": "safe-pass",
        "command": "python -c \"import sys; sys.exit(0)\"",
        "mandatory": True,
    }]
    task = simple_task(
        "t-amend",
        expected_paths=[{
            "path": "src/base.py", "type": "file",
            "required": True, "non_empty": True,
        }],
        acceptance_criteria=["base output exists"],
        deterministic_checks=[],
        contract_amendment_policy={
            "enabled": True,
            "allowed_kinds": ["required_output"],
            "allowed_paths": ["generated/**"],
        })
    seed_project(sandbox, [task])

    order = proj_order(task, allowed_paths=["src/base.py"])
    order["contract_amendments"] = [
        _proposal(
            "required_output",
            {"path": "generated/report.json", "type": "file",
             "required": True, "non_empty": True},
            "approved-report", "tool requires a generated report"),
        _proposal(
            "required_output", ".env",
            "rejected-env", "model requests an unauthorized file"),
    ]
    caller = FakeCaller({
        "conductor": order,
        "coder": worker_out(edits=[
            {"path": "src/base.py", "action": "write",
             "content": "VALUE = 1\n"},
            {"path": "generated/report.json", "action": "write",
             "content": "{\"ok\": true}\n"},
        ]),
        "qa": verifier_out("pass"),
    })

    result = run_cycle(cfg, caller=caller, clock=Clock())

    assert result["status"] == "success", result
    feature_status = FeatureGateRegistry(
        str(sandbox["agentic"] / "memory"), cfg).status(
            "contract_amendments")
    assert feature_status["successes"] == 1
    assert feature_status["effective_state"] == "canary"
    run_dir = os.path.join(
        str(sandbox["agentic"]), "runs", "cycle-" + result["run_id"])
    with open(os.path.join(run_dir, "contract-amendments.json"),
              encoding="utf-8") as handle:
        ledger = json.load(handle)
    assert ledger["feature_gate"]["effective_state"] == "canary"
    assert ledger["feature_gate"]["active"] is True
    decisions = {item["id"]: item for item in ledger["decisions"]}
    assert decisions["approved-report"]["accepted"] is True
    assert decisions["rejected-env"]["accepted"] is False
    assert decisions["rejected-env"]["decision_code"] == \
        "path_not_authorized"

    with open(os.path.join(run_dir, "task-contract.json"),
              encoding="utf-8") as handle:
        compiled = json.load(handle)
    required = {item["path"] for item in compiled["required_outputs"]}
    assert required == {"src/base.py", "generated/report.json"}
    assert compiled["contract_authority"] == \
        "backlog+approved_amendments"

    project_worktree = os.path.join(
        str(sandbox["agentic"]), "worktrees", "project")
    assert os.path.exists(os.path.join(
        project_worktree, "generated", "report.json"))
    assert not os.path.exists(os.path.join(project_worktree, ".env"))


def test_architect_schema_preserves_bounded_amendment_policy():
    schema_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        ".agentic", "schemas", "architect.schema.json")
    with open(schema_path, encoding="utf-8") as handle:
        schema = json.load(handle)

    task_properties = schema["properties"]["backlog"]["items"]["properties"]
    policy = task_properties["contract_amendment_policy"]
    assert policy["additionalProperties"] is False
    assert policy["required"] == ["enabled", "allowed_kinds"]
    assert set(policy["properties"]["allowed_kinds"]["items"]["enum"]) == {
        "required_output", "allowed_path",
        "deterministic_check", "acceptance_criterion",
    }


def test_architect_prompt_requires_explicit_plan_authority_for_policy():
    prompt_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        ".agentic", "prompts", "architect.md")
    with open(prompt_path, encoding="utf-8") as handle:
        prompt = handle.read()

    assert "OMIT by default" in prompt
    assert "source plan explicitly declares" in prompt
    assert "preferred low-risk shadow/canary probe" in prompt
    normalized_prompt = " ".join(prompt.split())
    assert "never use it to weaken, remove, or replace" in normalized_prompt


def test_conductor_prompt_requires_one_enabled_shadow_probe():
    prompt_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        ".agentic", "prompts", "project-conductor.md")
    with open(prompt_path, encoding="utf-8") as handle:
        prompt = " ".join(handle.read().split())

    assert "shadow mode" in prompt
    assert "MUST emit exactly one conservative proposal" in prompt
    assert "required observation probe" in prompt


def test_cycle_rejects_missing_required_shadow_probe_before_coder(sandbox):
    cfg = project_cfg(sandbox)
    cfg["advanced_features"] = {
        "contract_amendments": {
            "state": "shadow",
            "promotion_min_successes": 3,
        }}
    task = simple_task(
        "t-shadow-probe",
        contract_amendment_policy={
            "enabled": True,
            "allowed_kinds": ["acceptance_criterion"],
            "allowed_paths": [],
            "allowed_commands": [],
        })
    seed_project(sandbox, [task])
    caller = FakeCaller({"conductor": proj_order(task)})

    result = run_cycle(cfg, caller=caller, clock=Clock())

    assert result["status"] == "failure"
    assert result["detail"] == (
        "conductor omitted required shadow contract-amendment probe")
    assert [call for call in caller.calls if call["role"] == "coder"] == []
    current = {item["id"]: item for item in projstate.load_backlog(
        str(sandbox["agentic"]))}
    assert current[task["id"]]["status"] == "pending"
