"""MP Phase 4 — fleet scheduler: slot pools, fairness, waiting reasons,
pauses, restart recovery, no double execution, release on failure."""
import datetime
import json
import os

import pytest

from conftest import Clock
from core import fleet, projstate
from core.fleet import (SlotManager, classify_project, concurrency_config,
                        plan, run_tick, set_global_pause)
from core.registry import ProjectRegistry


@pytest.fixture
def world(tmp_path, base_cfg):
    """Four registered, initialised, enabled projects with architected
    backlogs — ready for scheduling."""
    registry = ProjectRegistry(home=str(tmp_path / "home"))
    cfg = dict(base_cfg)
    cfg["backends"] = {
        "claude": {"type": "api", "provider": "mock", "model": "m"},
        "codex": {"type": "api", "provider": "mock", "model": "m"},
        "ollama": {"type": "local", "model": "qwen3.5", "cost_free": True},
    }
    cfg["routing"] = {"mode": "simple", "primary": "claude",
                      "fallbacks": ["codex"]}
    cfg["concurrency"] = {"maximum_active_projects": 4,
                          "maximum_model_calls": 2,
                          "per_backend": {"claude": 1, "codex": 1,
                                          "ollama": 1}}
    ids = []
    for i, name in enumerate(["alpha", "beta", "gamma", "delta"]):
        root = tmp_path / "apps" / name
        root.mkdir(parents=True)
        (root / "plan.md").write_text("# plan\n", encoding="utf-8")
        record = registry.add(name, str(root), priority=50)
        registry.update(record["id"], enabled=True, status="initialised")
        state_dir = registry.project_runtime_dir(record["id"])
        registry.ensure_runtime_dirs(record["id"])
        projstate.save_backlog(state_dir, [projstate.normalize_task(
            {"id": "t1-%s" % name, "milestone": "m1",
             "description": "task", "skill": "app"})])
        projstate.write_yaml(state_dir, "milestones.yaml",
                             {"milestones": [{"id": "m1", "title": "m"}]})
        projstate.write_yaml(state_dir, "blockers.yaml", {"blockers": []})
        projstate.refresh_progress(state_dir)
        ids.append(record["id"])
    return {"registry": registry, "cfg": cfg, "ids": ids,
            "home": registry.home, "clock": Clock()}


def routed(world, backend_per_project=None):
    """Give each project its own backend so per-backend limits don't
    collapse the test unintentionally."""
    per = backend_per_project or {}
    for pid in world["ids"]:
        record = world["registry"].get(pid)
        profile = {"routing": {"mode": "simple",
                               "primary": per.get(pid, "claude"),
                               "fallbacks": []}}
        world["registry"].update(pid, backend_profile=profile)


# -- planning -----------------------------------------------------------------------

def test_model_slots_bound_concurrent_starts(world):
    routed(world, {world["ids"][0]: "claude", world["ids"][1]: "codex",
                   world["ids"][2]: "claude", world["ids"][3]: "codex"})
    decisions = plan(world["cfg"], world["registry"],
                     clock=world["clock"], home=world["home"])
    assert len(decisions["start"]) == 2          # maximum_model_calls: 2
    reasons = {w["project"]: w["reason"] for w in decisions["waiting"]}
    assert len(reasons) == 2
    assert all("slot" in r for r in reasons.values())


def test_per_backend_slots(world):
    routed(world)                                # everyone wants claude
    decisions = plan(world["cfg"], world["registry"],
                     clock=world["clock"], home=world["home"])
    assert len(decisions["start"]) == 1          # per_backend claude: 1
    waiting_reasons = [w["reason"] for w in decisions["waiting"]]
    assert any("claude slot" in r for r in waiting_reasons)


def test_cooling_project_releases_slot_for_others(world):
    routed(world, {world["ids"][0]: "claude", world["ids"][1]: "codex"})
    from core.scheduler import Scheduler
    state_dir = world["registry"].project_runtime_dir(world["ids"][0])
    scheduler = Scheduler(world["cfg"], os.path.join(state_dir, "memory"),
                          clock=world["clock"])
    scheduler.start_cooling("success")
    decisions = plan(world["cfg"], world["registry"],
                     clock=world["clock"], home=world["home"])
    assert world["ids"][0] not in [s["project"] for s in decisions["start"]]
    cooling = [w for w in decisions["waiting"]
               if w["project"] == world["ids"][0]]
    assert cooling and "cooling until" in cooling[0]["reason"]
    # the cooling project does NOT consume a model slot
    assert any(s["project"] == world["ids"][1]
               for s in decisions["start"])


def test_fairness_least_recently_scheduled_first(world):
    routed(world, {pid: "claude" for pid in world["ids"]})
    first = plan(world["cfg"], world["registry"], clock=world["clock"],
                 home=world["home"])
    first_started = first["start"][0]["project"]
    SlotManager(world["home"], clock=world["clock"]).release(first_started)
    second = plan(world["cfg"], world["registry"], clock=world["clock"],
                  home=world["home"])
    # the already-served project rotates to the back
    assert second["start"][0]["project"] != first_started


def test_priority_respected(world):
    routed(world, {pid: "claude" for pid in world["ids"]})
    world["registry"].update(world["ids"][3], priority=90)
    decisions = plan(world["cfg"], world["registry"],
                     clock=world["clock"], home=world["home"])
    assert decisions["start"][0]["project"] == world["ids"][3]


def test_global_pause_blocks_everything(world):
    set_global_pause(world["home"], True)
    decisions = plan(world["cfg"], world["registry"],
                     clock=world["clock"], home=world["home"])
    assert decisions["start"] == []
    assert all(w["reason"] == "global pause active"
               for w in decisions["waiting"])
    set_global_pause(world["home"], False)
    routed(world, {world["ids"][0]: "claude"})
    decisions2 = plan(world["cfg"], world["registry"],
                      clock=world["clock"], home=world["home"])
    assert decisions2["start"]


def test_per_project_pause(world):
    routed(world, {world["ids"][0]: "claude", world["ids"][1]: "codex"})
    from core.scheduler import Scheduler
    state_dir = world["registry"].project_runtime_dir(world["ids"][0])
    Scheduler(world["cfg"], os.path.join(state_dir, "memory"),
              clock=world["clock"]).pause()
    decisions = plan(world["cfg"], world["registry"],
                     clock=world["clock"], home=world["home"])
    assert decisions["states"][world["ids"][0]] == "paused"
    assert world["ids"][0] not in [s["project"]
                                   for s in decisions["start"]]


def test_no_double_execution(world):
    routed(world, {world["ids"][0]: "claude"})
    slots = SlotManager(world["home"], clock=world["clock"])
    ok, _ = slots.acquire(world["cfg"], world["ids"][0], "claude")
    assert ok
    decisions = plan(world["cfg"], world["registry"],
                     clock=world["clock"], home=world["home"])
    assert world["ids"][0] not in [s["project"]
                                   for s in decisions["start"]]
    mine = [w for w in decisions["waiting"]
            if w["project"] == world["ids"][0]]
    assert mine and "already holds" in mine[0]["reason"] \
        or any("slot" in w["reason"] for w in mine)


def test_restart_recovery_reaps_dead_allocations(world):
    slots = SlotManager(world["home"], clock=world["clock"])
    dead = {"slots": {"model": 1, "backend:claude": 1},
            "backend": "claude", "pid": 999999,
            "machine": fleet._machine(),
            "acquired_at": "2026-07-15T11:00:00",
            "expires_at": "2026-07-15T14:00:00"}
    os.makedirs(world["home"], exist_ok=True)
    with open(slots.path, "w", encoding="utf-8") as fh:
        json.dump({"ghost-project": dead}, fh)
    assert slots.usage()["active_projects"] == 0    # dead pid reaped
    expired = dict(dead, pid=os.getpid(),
                   expires_at="2026-07-15T11:30:00")
    with open(slots.path, "w", encoding="utf-8") as fh:
        json.dump({"stale-project": expired}, fh)
    assert slots.usage()["active_projects"] == 0    # expiry reaped


def test_slots_released_after_runner_failure(world):
    routed(world, {world["ids"][0]: "claude", world["ids"][1]: "codex"})

    def exploding_runner(cfg, registry, project_id):
        raise RuntimeError("boom in %s" % project_id)

    result = run_tick(world["cfg"], world["registry"],
                      runner=exploding_runner, clock=world["clock"],
                      home=world["home"])
    assert result["start"]
    assert all(r["status"] == "failure" for r in result["results"].values())
    slots = SlotManager(world["home"], clock=world["clock"])
    assert slots.usage()["active_projects"] == 0


def test_run_tick_executes_started_projects_only(world):
    routed(world, {world["ids"][0]: "claude", world["ids"][1]: "codex",
                   world["ids"][2]: "claude", world["ids"][3]: "codex"})
    ran = []

    def recording_runner(cfg, registry, project_id):
        ran.append(project_id)
        return {"status": "success"}

    result = run_tick(world["cfg"], world["registry"],
                      runner=recording_runner, clock=world["clock"],
                      home=world["home"])
    assert sorted(ran) == sorted(s["project"] for s in result["start"])
    assert len(ran) == 2


def test_states_and_decisions_persisted(world):
    routed(world, {world["ids"][0]: "claude"})
    plan(world["cfg"], world["registry"], clock=world["clock"],
         home=world["home"])
    decisions = fleet.read_decisions(world["home"])
    assert decisions and "states" in decisions[-1]
    for state in decisions[-1]["states"].values():
        assert state in fleet.PROJECT_STATES


def test_classify_uninitialised_and_ready(world, base_cfg):
    registry = world["registry"]
    fresh_root = os.path.join(os.path.dirname(
        registry.get(world["ids"][0])["root_path"]), "fresh")
    os.makedirs(fresh_root)
    record = registry.add("fresh", fresh_root)
    state, reason = classify_project(world["cfg"], registry, record)
    assert state == "uninitialised"
    registry.update(record["id"], status="initialised")
    state, reason = classify_project(world["cfg"], registry,
                                     registry.get(record["id"]))
    assert state == "ready"


def test_concurrency_config_merges():
    merged = concurrency_config({"concurrency": {
        "maximum_model_calls": 5, "per_backend": {"claude": 3}}})
    assert merged["maximum_model_calls"] == 5
    assert merged["per_backend"]["claude"] == 3
    assert merged["per_backend"]["codex"] == 1      # defaults preserved
    assert merged["maximum_active_projects"] == 4