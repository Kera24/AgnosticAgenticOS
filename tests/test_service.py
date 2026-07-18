"""MP Phase 10 — service lifecycle: single instance, health wait, port
handling, browser fallback, stop/restart/status/logs, pause-on-start.
Everything injected — no processes spawned, no sockets opened."""
import json
import os

from core import service
from core.fleet import load_fleet_state


class World:
    """Injectable process world."""

    def __init__(self, home):
        self.home = home
        self.spawned = []
        self.killed = []
        self.alive = set()
        self.health_ok = set()
        self.opened = []
        self.next_pid = 1000

    def spawner(self, port, home):
        self.next_pid += 1
        self.spawned.append({"port": port, "pid": self.next_pid})
        self.alive.add(self.next_pid)
        self.health_ok.add(port)
        return self.next_pid

    def health(self, port, timeout=3):
        return {"ok": True} if port in self.health_ok else None

    def opener(self, url):
        self.opened.append(url)

    def failing_opener(self, url):
        raise RuntimeError("no default browser")

    def terminator(self, pid):
        self.killed.append(pid)
        self.alive.discard(pid)

    def prober(self, port):
        return port not in {p["port"] for p in self.spawned}


def patch_alive(monkeypatch, world):
    monkeypatch.setattr(service, "_pid_alive",
                        lambda pid: pid in world.alive)


def test_start_spawns_waits_and_opens(tmp_path, base_cfg, monkeypatch):
    world = World(str(tmp_path))
    patch_alive(monkeypatch, world)
    result = service.start(base_cfg, home=str(tmp_path),
                           spawner=world.spawner, health=world.health,
                           opener=world.opener, prober=world.prober,
                           poll_interval=0)
    assert result["status"] == "started"
    assert result["url"] == "http://127.0.0.1:8765"
    assert world.opened == ["http://127.0.0.1:8765"]
    state = service.read_state(str(tmp_path))
    assert state["pid"] == result["pid"] and state["port"] == 8765


def test_single_instance_no_double_spawn(tmp_path, base_cfg, monkeypatch):
    world = World(str(tmp_path))
    patch_alive(monkeypatch, world)
    first = service.start(base_cfg, home=str(tmp_path),
                          spawner=world.spawner, health=world.health,
                          opener=world.opener, prober=world.prober,
                          poll_interval=0)
    second = service.start(base_cfg, home=str(tmp_path),
                           spawner=world.spawner, health=world.health,
                           opener=world.opener, prober=world.prober,
                           no_open=True, poll_interval=0)
    assert second["status"] == "already_running"
    assert second["pid"] == first["pid"]
    assert len(world.spawned) == 1


def test_port_conflict_picks_next(tmp_path, base_cfg, monkeypatch):
    world = World(str(tmp_path))
    patch_alive(monkeypatch, world)
    busy = {8765, 8766}
    result = service.start(base_cfg, home=str(tmp_path),
                           spawner=world.spawner, health=world.health,
                           opener=world.opener,
                           prober=lambda p: p not in busy,
                           no_open=True, poll_interval=0)
    assert result["port"] == 8767


def test_browser_failure_is_not_fatal(tmp_path, base_cfg, monkeypatch):
    world = World(str(tmp_path))
    patch_alive(monkeypatch, world)
    result = service.start(base_cfg, home=str(tmp_path),
                           spawner=world.spawner, health=world.health,
                           opener=world.failing_opener,
                           prober=world.prober, poll_interval=0)
    assert result["status"] == "started"
    assert result["browser_opened"] is False
    assert "manually" in result["note"]


def test_startup_failure_reports_log_path(tmp_path, base_cfg,
                                          monkeypatch):
    world = World(str(tmp_path))
    patch_alive(monkeypatch, world)

    def dying_spawner(port, home):
        pid = world.spawner(port, home)
        world.alive.discard(pid)          # process exits immediately
        world.health_ok.discard(port)
        return pid

    result = service.start(base_cfg, home=str(tmp_path),
                           spawner=dying_spawner,
                           health=lambda p, timeout=3: None,
                           opener=world.opener, prober=world.prober,
                           poll_interval=0, wait_seconds=1)
    assert result["status"] == "failed"
    assert "service.log" in result["detail"]
    assert service.read_state(str(tmp_path)) is None


def test_stop_and_status(tmp_path, base_cfg, monkeypatch):
    world = World(str(tmp_path))
    patch_alive(monkeypatch, world)
    started = service.start(base_cfg, home=str(tmp_path),
                            spawner=world.spawner, health=world.health,
                            opener=world.opener, prober=world.prober,
                            no_open=True, poll_interval=0)
    report = service.status(base_cfg, home=str(tmp_path),
                            health=world.health)
    assert report["service"]["healthy"] is True
    assert report["global_pause"] is False
    # graceful path first: the injected shutdowner succeeds
    def graceful_shutdowner(port, timeout=3):
        world.alive.discard(started["pid"])
        return True

    stopped = service.stop(home=str(tmp_path),
                           terminator=world.terminator,
                           shutdowner=graceful_shutdowner)
    assert stopped["status"] == "stopped"
    assert stopped["pid"] == started["pid"]
    assert stopped["graceful"] is True
    assert world.killed == []                # never force-killed
    report2 = service.status(base_cfg, home=str(tmp_path),
                             health=world.health)
    assert report2["service"] == {"status": "not_running"}
    assert service.stop(home=str(tmp_path),
                        shutdowner=lambda p, timeout=3: False) \
        == {"status": "not_running"}


def test_stop_falls_back_to_terminate(tmp_path, base_cfg, monkeypatch):
    world = World(str(tmp_path))
    patch_alive(monkeypatch, world)
    started = service.start(base_cfg, home=str(tmp_path),
                            spawner=world.spawner, health=world.health,
                            opener=world.opener, prober=world.prober,
                            no_open=True, poll_interval=0)
    stopped = service.stop(home=str(tmp_path),
                           terminator=world.terminator,
                           shutdowner=lambda p, timeout=3: False)
    assert stopped["graceful"] is False
    assert world.killed == [started["pid"]]


def test_crash_recovery_clears_stale_state(tmp_path, base_cfg,
                                           monkeypatch):
    world = World(str(tmp_path))
    patch_alive(monkeypatch, world)
    os.makedirs(str(tmp_path), exist_ok=True)
    with open(os.path.join(str(tmp_path), "service.json"), "w") as fh:
        json.dump({"pid": 424242, "port": 8765,
                   "url": "http://127.0.0.1:8765"}, fh)
    assert service.running_service(str(tmp_path),
                                   health=world.health) is None
    result = service.start(base_cfg, home=str(tmp_path),
                           spawner=world.spawner, health=world.health,
                           opener=world.opener, prober=world.prober,
                           no_open=True, poll_interval=0)
    assert result["status"] == "started"     # stale state did not block


def test_start_paused_sets_global_pause(tmp_path, base_cfg, monkeypatch):
    world = World(str(tmp_path))
    patch_alive(monkeypatch, world)
    service.start(base_cfg, home=str(tmp_path), spawner=world.spawner,
                  health=world.health, opener=world.opener,
                  prober=world.prober, no_open=True, paused=True,
                  poll_interval=0)
    assert load_fleet_state(str(tmp_path))["global_pause"] is True


def test_start_with_project_enables_it(tmp_path, base_cfg, monkeypatch):
    from core.registry import ProjectRegistry
    world = World(str(tmp_path))
    patch_alive(monkeypatch, world)
    registry = ProjectRegistry(home=str(tmp_path))
    app = tmp_path / "app"
    app.mkdir()
    record = registry.add("solo", str(app))
    service.start(base_cfg, home=str(tmp_path), spawner=world.spawner,
                  health=world.health, opener=world.opener,
                  prober=world.prober, no_open=True,
                  project=record["id"], poll_interval=0)
    assert registry.get(record["id"])["enabled"] is True
    # unknown project id -> warning, not a crash
    result = service.start(base_cfg, home=str(tmp_path),
                           spawner=world.spawner, health=world.health,
                           opener=world.opener, prober=world.prober,
                           no_open=True, project="ghost",
                           poll_interval=0)
    assert result["status"] == "already_running"


def test_logs_tail(tmp_path):
    paths = service._paths(str(tmp_path))
    os.makedirs(paths["logs"], exist_ok=True)
    with open(paths["log_file"], "w", encoding="utf-8") as fh:
        fh.write("".join("line %d\n" % i for i in range(100)))
    report = service.logs(home=str(tmp_path), lines=10)
    assert len(report["lines"]) == 10
    assert report["lines"][-1] == "line 99"
    assert service.logs(home=str(tmp_path / "empty"))["lines"] == []
