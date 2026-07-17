"""Phase 7 — review/repair council: bounded review rounds, repair packets,
failure fingerprints, escalation, specialist routing, handoff bounds."""
from conftest import (Clock, FakeCaller, project_cfg, proj_order,
                      seed_project, simple_task, verifier_out, worker_out)
from core import projstate
from core.project import _worker_role, run_cycle


def qa_pass():
    return verifier_out("pass")


def sec_pass():
    return {"verdict": "pass", "concerns": [], "reason": "clean"}


def failing_check(sandbox):
    sandbox["cfg"]["verification"]["commands"] = [
        {"name": "always-fails",
         "command": ["python", "-c", "import sys; sys.exit(1)"],
         "mandatory": True}]
    from core import gate
    gate.save_baseline(str(sandbox["agentic"]),
                       [{"name": "always-fails", "passed": True}])


# -- failure fingerprints ------------------------------------------------------------

def test_identical_repair_short_circuits(sandbox):
    """Same diff + same failure twice -> stop early instead of burning the
    remaining budget on a guaranteed-identical outcome."""
    project_cfg(sandbox)
    failing_check(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    caller = FakeCaller({"conductor": proj_order(task),
                         "coder": worker_out(),      # identical every time
                         "qa": qa_pass(), "security": sec_pass()})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "failure"
    assert "repeated identical failure" in result["detail"]
    coder_calls = [c for c in caller.calls if c["role"] == "coder"]
    assert len(coder_calls) == 2          # not 3: fingerprint stopped it
    tasks = projstate.load_backlog(str(sandbox["agentic"]))
    assert tasks[0]["status"] == "blocked"
    assert "identical" in tasks[0]["blocking_reason"]


# -- bounded model-review rounds -------------------------------------------------------

def test_review_rounds_bounded_and_escalate(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    # coder produces a different fix each round; QA never accepts
    coders = [worker_out(),
              worker_out(edits=[{"path": "src/app.py", "action": "write",
                                 "content": "VALUE = 2  # v2\n"}]),
              worker_out(edits=[{"path": "src/app.py", "action": "write",
                                 "content": "VALUE = 2  # v3\n"}])]
    caller = FakeCaller({"conductor": proj_order(task), "coder": coders,
                         "qa": verifier_out("fail"),
                         "security": sec_pass()})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "failure"
    assert "review rounds" in result["detail"]
    # initial + default 2 repair rounds = 3 coder calls
    assert len([c for c in caller.calls if c["role"] == "coder"]) == 3
    tasks = projstate.load_backlog(str(sandbox["agentic"]))
    assert tasks[0]["status"] == "blocked"
    assert tasks[0]["blocking_reason"].startswith("QA:")


def test_review_rounds_configurable(sandbox):
    project_cfg(sandbox)
    sandbox["cfg"]["cycle"] = {"maximum_review_rounds": 1}
    task = simple_task()
    seed_project(sandbox, [task])
    caller = FakeCaller({"conductor": proj_order(task),
                         "coder": [worker_out(),
                                   worker_out(edits=[
                                       {"path": "src/app.py",
                                        "action": "write",
                                        "content": "VALUE = 9\n"}])],
                         "qa": verifier_out("fail"), "security": sec_pass()})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "failure"
    assert len([c for c in caller.calls if c["role"] == "coder"]) == 2


def test_repair_packet_is_structured_not_conversation(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    rejection = verifier_out("fail")
    rejection["required_repairs"] = ["rename VALUE to COUNT",
                                     "add a docstring"]
    rejection["findings"] = ["naming unclear"]
    caller = FakeCaller({"conductor": proj_order(task),
                         "coder": [worker_out(),
                                   worker_out(edits=[
                                       {"path": "src/app.py",
                                        "action": "write",
                                        "content": "COUNT = 2\n"}])],
                         "qa": [rejection, qa_pass(), qa_pass()],
                         "security": sec_pass()})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "success"
    repair_call = [c for c in caller.calls if c["role"] == "coder"][1]
    packet = repair_call["input"]
    assert packet["required_repairs"] == ["rename VALUE to COUNT",
                                          "add a docstring"]
    assert packet["review_findings"] == ["naming unclear"]
    # never the reviewer's whole output — just the structured packet
    assert "done_when_results" not in packet
    assert "verdict" not in packet


# -- deterministic gate supremacy -------------------------------------------------------

def test_model_approval_cannot_override_deterministic_failure(sandbox):
    """Even a QA 'pass' cannot complete a task whose checks fail: the gate
    runs first and repairs/blocks before QA is ever consulted."""
    project_cfg(sandbox)
    failing_check(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    caller = FakeCaller({"conductor": proj_order(task),
                         "coder": worker_out(), "qa": qa_pass(),
                         "security": sec_pass()})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "failure"
    assert not [c for c in caller.calls if c["role"] == "qa"]
    tasks = projstate.load_backlog(str(sandbox["agentic"]))
    assert tasks[0]["status"] == "blocked"


# -- specialist worker routing ---------------------------------------------------------

def test_ui_tasks_route_to_ui_designer():
    assert _worker_role({"kind": "ui"}, {}) == "ui_designer"
    assert _worker_role({"expected_paths": ["ui/src/**"]}, {}) \
        == "ui_designer"
    assert _worker_role({}, {"allowed_paths": ["src/App.tsx"]}) \
        == "ui_designer"
    assert _worker_role({"expected_paths": ["src/**"]},
                        {"allowed_paths": ["src/app.py"]}) == "coder"


def test_ui_designer_invoked_for_ui_task(sandbox):
    project_cfg(sandbox)
    task = simple_task(expected_paths=["ui/**"], kind="ui")
    seed_project(sandbox, [task])
    order = proj_order(task, allowed_paths=["ui/app.css"])
    caller = FakeCaller({
        "conductor": order,
        "ui_designer": worker_out(edits=[{"path": "ui/app.css",
                                          "action": "write",
                                          "content": "body{margin:0}\n"}]),
        "qa": qa_pass(), "security": sec_pass()})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "success"
    assert [c for c in caller.calls if c["role"] == "ui_designer"]
    assert not [c for c in caller.calls if c["role"] == "coder"]


# -- handoff bounds ---------------------------------------------------------------------

def test_each_backend_tried_at_most_once_on_exhaustion(sandbox):
    project_cfg(sandbox)
    task = simple_task()
    seed_project(sandbox, [task])
    caller = FakeCaller({
        "conductor": proj_order(task),
        "coder": [{"_error": "usage_limit", "backend": "mock"},
                  {"_error": "usage_limit", "backend": "mock2"},
                  worker_out()],
        "qa": qa_pass(), "security": sec_pass()})
    result = run_cycle(sandbox["cfg"], caller=caller, clock=Clock())
    assert result["status"] == "usage_limit"
    # mock failed, handed to mock2, mock2 failed, chain exhausted: exactly 2
    assert len([c for c in caller.calls if c["role"] == "coder"]) == 2
