"""Phase 9 — dynamic capacity/scheduling: review reserve, confidence
policy, failure-streak cooling, window envelope, deferral, override."""
import datetime

from conftest import Clock, project_cfg
from core.breaker import BreakerBoard
from core.capacity import (CapacityLedger, capacity_config, decide_start,
                           estimate_cycle_tokens)
from core.scheduler import Scheduler


def cfg_with(sched_capacity=None, cooling=None, window=None):
    cfg = project_cfg({"cfg": {"project": {"name": "t"}}})
    cfg["scheduler"]["capacity"] = sched_capacity or {}
    if cooling:
        cfg["scheduler"]["cooling"].update(cooling)
    if window:
        cfg["scheduler"]["operating_window"] = window
    return cfg


def test_capacity_config_merges_scheduler_overrides():
    cfg = cfg_with({"safety_multiplier": 2.0,
                    "confidence_required": True})
    merged = capacity_config(cfg)
    assert merged["safety_multiplier"] == 2.0
    assert merged["confidence_required"] is True
    assert merged["include_review_reserve"] is True


def test_review_reserve_included_and_excludable(tmp_path):
    cfg = cfg_with()
    ledger = CapacityLedger(cfg, str(tmp_path))
    with_reserve = estimate_cycle_tokens(cfg, {"expected_size": "medium"},
                                         ledger, "mock")
    assert with_reserve["review_reserve_tokens"] > 0
    cfg2 = cfg_with({"include_review_reserve": False})
    without = estimate_cycle_tokens(cfg2, {"expected_size": "medium"},
                                    CapacityLedger(cfg2, str(tmp_path)),
                                    "mock")
    assert without["review_reserve_tokens"] == 0
    assert without["required_capacity_tokens"] \
        < with_reserve["required_capacity_tokens"]


def test_insufficient_reported_capacity_defers(tmp_path):
    clock = Clock()
    cfg = cfg_with()
    ledger = CapacityLedger(cfg, str(tmp_path), clock=clock)
    board = BreakerBoard(str(tmp_path), clock=clock)
    decision = decide_start(cfg, {"expected_size": "large"}, ledger, board,
                            ["mock"], reported_remaining={"mock": 10})
    # reported capacity too small and no other backend -> human_required
    assert decision["decision"] == "human_required"


def test_stop_before_exhaustion_disabled_proceeds(tmp_path):
    clock = Clock()
    cfg = cfg_with({"stop_before_exhaustion": False})
    ledger = CapacityLedger(cfg, str(tmp_path), clock=clock)
    board = BreakerBoard(str(tmp_path), clock=clock)
    decision = decide_start(cfg, {}, ledger, board, ["mock"],
                            reported_remaining={"mock": 10})
    assert decision["decision"] == "start"
    assert "stop_before_exhaustion" in decision["reason"]


def test_confidence_required_blocks_unknown_capacity(tmp_path):
    clock = Clock()
    cfg = cfg_with({"confidence_required": True})
    ledger = CapacityLedger(cfg, str(tmp_path), clock=clock)
    board = BreakerBoard(str(tmp_path), clock=clock)
    decision = decide_start(cfg, {}, ledger, board, ["mock"])
    assert decision["decision"] == "human_required"
    assert "confidence" in decision["reason"]


def test_failure_streak_escalates_cooling(tmp_path):
    clock = Clock()
    scheduler = Scheduler(cfg_with(), str(tmp_path), clock=clock)
    first = scheduler.cooldown_minutes("failure", failure_streak=1)
    assert first == 30
    scheduler.start_cooling("failure")     # streak 1
    scheduler.start_cooling("failure")     # streak 2 -> 60 min
    assert scheduler.state["failure_streak"] == 2
    until = datetime.datetime.fromisoformat(scheduler.state["next_run_at"])
    assert (until - clock.now).total_seconds() == 60 * 60
    scheduler.start_cooling("failure")     # streak 3 -> 120 min
    until = datetime.datetime.fromisoformat(scheduler.state["next_run_at"])
    assert (until - clock.now).total_seconds() == 120 * 60
    scheduler.start_cooling("success")     # success resets
    assert scheduler.state["failure_streak"] == 0


def test_dynamic_cooling_can_be_disabled(tmp_path):
    scheduler = Scheduler(cfg_with(cooling={"dynamic": False}),
                          str(tmp_path), clock=Clock())
    assert scheduler.cooldown_minutes("failure", failure_streak=5) == 30


def test_cycle_envelope_must_fit_operating_window(tmp_path):
    clock = Clock()                        # 12:00
    scheduler = Scheduler(cfg_with(window={"enabled": True,
                                           "start": "07:00",
                                           "stop": "12:20"}),
                          str(tmp_path), clock=clock)
    ok, reason = scheduler.eligible()      # no envelope: inside window
    assert ok
    ok, reason = scheduler.eligible(cycle_minutes=30)
    assert not ok and "insufficient operating window" in reason
    ok, _ = scheduler.eligible(cycle_minutes=15)
    assert ok


def test_deferred_reason_persisted_and_restart_safe(tmp_path):
    clock = Clock()
    scheduler = Scheduler(cfg_with(), str(tmp_path), clock=clock)
    scheduler.defer("no backend currently available",
                    required_estimated_tokens=54321,
                    confidence="estimated", until="2026-07-15T13:00:00")
    fresh = Scheduler(cfg_with(), str(tmp_path), clock=clock)   # restart
    assert fresh.state["state"] == "cooling"
    deferred = fresh.state["deferred"]
    assert deferred["reason"] == "no backend currently available"
    assert deferred["required_estimated_tokens"] == 54321
    assert deferred["capacity_confidence"] == "estimated"
    assert fresh.state["next_run_at"] == "2026-07-15T13:00:00"


def test_manual_override_clears_cooling(tmp_path):
    clock = Clock()
    scheduler = Scheduler(cfg_with(), str(tmp_path), clock=clock)
    scheduler.start_cooling("failure")
    assert not scheduler.eligible()[0]
    scheduler.resume(force=True)
    assert scheduler.eligible()[0]
    assert scheduler.state["next_run_at"] is None
    # plain resume does NOT override cooling
    scheduler.start_cooling("failure")
    scheduler.resume()
    assert not scheduler.eligible()[0]
