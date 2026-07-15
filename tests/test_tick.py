"""End-to-end orchestration with mocked models: dry-run, full pass,
deterministic-gate failure, worker/verifier disagreement, changed-line limit,
protected-path rejection at edit time, and preservation of unrelated
working-tree changes."""
import json

from conftest import (FakeInvoker, order_out, triage_out, verifier_out,
                      worker_out)
from core.orchestrator import run_tick


def tick(sandbox, invoker, dry_run=False, run_id=None):
    return run_tick(cfg=sandbox["cfg"], dry_run=dry_run, invoker=invoker,
                    run_id=run_id)


def queue_items(sandbox):
    qdir = sandbox["agentic"] / "queue"
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(qdir.glob("*.json"))]


# 20. dry-run mode ------------------------------------------------------------
def test_dry_run_produces_order_and_edits_nothing(sandbox):
    invoker = FakeInvoker({"triage": triage_out(), "conductor": order_out()})
    result = tick(sandbox, invoker, dry_run=True)
    assert result["status"] == "dry_run"
    assert result["work_order"]["skill"] == "fix-lint-debt"
    assert [c["role"] for c in invoker.calls] == ["triage", "conductor"]
    assert (sandbox["repo"] / "src" / "app.py").read_text() == "VALUE = 1\n"
    assert not list((sandbox["agentic"] / "worktrees").iterdir())


def test_quiet_triage_stops(sandbox):
    invoker = FakeInvoker({"triage": {"status": "quiet", "findings": []}})
    assert tick(sandbox, invoker)["status"] == "quiet"
    assert [c["role"] for c in invoker.calls] == ["triage"]


# full pass -> verified draft queued (review mode) ------------------------------
def test_full_pass_creates_verified_draft(sandbox):
    invoker = FakeInvoker({"triage": triage_out(), "conductor": order_out(),
                           "worker": worker_out(),
                           "verifier": verifier_out("pass")})
    result = tick(sandbox, invoker, run_id="t-pass")
    assert result["status"] == "draft_ready"
    assert result["branch"] == "agentic/t-pass"
    # verified work waits for approval; nothing merged to main
    items = queue_items(sandbox)
    assert items and items[-1]["type"] == "verified_draft"
    # user's tree untouched
    assert (sandbox["repo"] / "src" / "app.py").read_text() == "VALUE = 1\n"
    # the draft branch contains the change
    import subprocess
    show = subprocess.run(["git", "show", "agentic/t-pass:src/app.py"],
                          cwd=sandbox["repo"], capture_output=True, text=True)
    assert show.stdout == "VALUE = 2\n"
    # trust recorded a pass
    trust = (sandbox["agentic"] / "memory" / "trust.tsv").read_text()
    assert "fix-lint-debt\t1\t1\t0" in trust
    # verifier received the diff and check results, never the worker's words
    vcall = [c for c in invoker.calls if c["role"] == "verifier"][0]
    assert "diff" in vcall["input"] and "deterministic_checks" in vcall["input"]
    assert "worker" not in json.dumps(vcall["input"]).lower() or \
        "summary" not in vcall["input"]


# 18. failed deterministic gate blocks completion --------------------------------
def test_gate_failure_fails_task_and_logs_trust_failure(sandbox):
    sandbox["cfg"]["verification"]["commands"] = [
        {"name": "always-fails",
         "command": "python -c \"import sys; sys.exit(1)\"", "mandatory": True}]
    # record a healthy baseline first so the failure counts as a regression
    from core import gate
    gate.save_baseline(str(sandbox["agentic"]),
                       [{"name": "always-fails", "passed": True}])
    invoker = FakeInvoker({"triage": triage_out(), "conductor": order_out(),
                           "worker": worker_out(),
                           "verifier": verifier_out("pass")})
    result = tick(sandbox, invoker, run_id="t-gatefail")
    assert result["status"] == "failed"
    trust = (sandbox["agentic"] / "memory" / "trust.tsv").read_text()
    assert "fix-lint-debt\t1\t0\t1" in trust      # gate failure == trust failure
    items = queue_items(sandbox)
    assert items[-1]["type"] == "failed_or_disputed"
    # worktree preserved for human review
    assert (sandbox["agentic"] / "worktrees" / "t-gatefail").exists()


# 19. worker/verifier disagreement -> human after 2 -------------------------------
def test_two_disagreements_queue_for_human(sandbox):
    scripted = {"triage": triage_out(), "conductor": order_out(),
                "worker": worker_out(), "verifier": verifier_out("fail")}
    r1 = tick(sandbox, FakeInvoker(scripted), run_id="t-dis1")
    assert r1["status"] == "needs_human"
    r2 = tick(sandbox, FakeInvoker(scripted), run_id="t-dis2")
    assert r2["status"] == "needs_human"
    disagreements = json.loads(
        (sandbox["agentic"] / "memory" / "disagreements.json").read_text())
    assert disagreements["fix-lint-debt"] == 2
    decisions = (sandbox["agentic"] / "memory" / "decisions.jsonl").read_text()
    assert "maker_verifier_disagreement" in decisions


def test_uncertain_verdict_is_never_a_pass(sandbox):
    invoker = FakeInvoker({"triage": triage_out(), "conductor": order_out(),
                           "worker": worker_out(),
                           "verifier": verifier_out("uncertain")})
    result = tick(sandbox, invoker, run_id="t-unc")
    assert result["status"] == "needs_human"
    assert not any(i["type"] == "verified_draft" for i in queue_items(sandbox))


# 14. changed-line limit ----------------------------------------------------------
def test_changed_line_limit_enforced_in_code(sandbox):
    big = "\n".join("line %d" % i for i in range(500)) + "\n"
    invoker = FakeInvoker({
        "triage": triage_out(),
        "conductor": order_out(maximum_changed_lines=400),
        "worker": worker_out(edits=[{"path": "src/app.py", "action": "write",
                                     "content": big}]),
        "verifier": verifier_out("pass")})
    result = tick(sandbox, invoker, run_id="t-big")
    assert result["status"] == "policy_violation"
    assert "exceed" in result["reason"]
    assert [c["role"] for c in invoker.calls].count("verifier") == 0


# 13. protected-path rejection at edit-application time -----------------------------
def test_worker_edit_to_protected_path_rejected(sandbox):
    invoker = FakeInvoker({
        "triage": triage_out(),
        "conductor": order_out(),
        "worker": worker_out(edits=[
            {"path": "src/app.py", "action": "write", "content": "VALUE = 2\n"},
            {"path": ".env", "action": "write", "content": "X=1\n"}]),
        "verifier": verifier_out("pass")})
    result = tick(sandbox, invoker, run_id="t-prot")
    assert result["status"] == "policy_violation"
    assert "protected" in result["reason"]
    assert not (sandbox["repo"] / ".env").exists()
    state = (sandbox["agentic"] / "memory" / "STATE.md").read_text()
    assert "protected_path" in state


# 21. preservation of unrelated working-tree changes --------------------------------
def test_unrelated_user_changes_preserved(sandbox):
    dirty = sandbox["repo"] / "tests_placeholder.txt"
    dirty.write_text("user edit in progress - do not lose\n", encoding="utf-8")
    invoker = FakeInvoker({"triage": triage_out(), "conductor": order_out(),
                           "worker": worker_out(),
                           "verifier": verifier_out("pass")})
    result = tick(sandbox, invoker, run_id="t-preserve")
    assert result["status"] == "draft_ready"
    assert dirty.read_text() == "user edit in progress - do not lose\n"
    assert (sandbox["repo"] / "src" / "app.py").read_text() == "VALUE = 1\n"


# worker blockers queue instead of inventing decisions ------------------------------
def test_worker_blocker_queues(sandbox):
    invoker = FakeInvoker({
        "triage": triage_out(), "conductor": order_out(),
        "worker": worker_out(blocked=True, edits=[],
                             blocker="needs STRIPE_ENDPOINT decision"),
        "verifier": verifier_out("pass")})
    result = tick(sandbox, invoker, run_id="t-block")
    assert result["status"] == "blocked"
    assert queue_items(sandbox)[-1]["type"] == "blocker"


# conductor queue action is honoured -------------------------------------------------
def test_conductor_queue_action(sandbox):
    invoker = FakeInvoker({
        "triage": triage_out(sensitive=True),
        "conductor": order_out(action="queue",
                               queue_reason="touches billing")})
    result = tick(sandbox, invoker, run_id="t-q")
    assert result["status"] == "queued"
    assert "billing" in queue_items(sandbox)[-1]["reason"]
