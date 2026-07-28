"""Phase 2: controlled, backlog-authorized contract amendments."""
from conftest import simple_task
from core import contract


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
