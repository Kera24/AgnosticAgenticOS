"""Background fleet worker wiring for the localhost service."""
from core import fleet_worker, service


class Registry:
    def __init__(self, home):
        self.home = str(home)


def test_worker_tick_uses_existing_fleet_engine(tmp_path, monkeypatch):
    calls = []
    events = []

    def fake_tick(cfg, registry, runner=None, home=None):
        calls.append((cfg, registry, runner, home))
        return {"start": [{"project": "app", "backend": "codex"}],
                "results": {"app": {"status": "success"}},
                "waiting": []}

    monkeypatch.setattr(fleet_worker.fleet, "run_tick", fake_tick)
    runner = object()
    worker = fleet_worker.FleetWorker(
        {}, registry=Registry(tmp_path), runner=runner,
        interval=5, logger=events.append)

    result = worker.tick()

    assert result["results"]["app"]["status"] == "success"
    assert calls == [({}, worker.registry, runner, str(tmp_path))]
    assert events[0]["event"] == "fleet_tick"


def test_worker_survives_tick_error_and_stops(tmp_path, monkeypatch):
    events = []
    worker = fleet_worker.FleetWorker(
        {}, registry=Registry(tmp_path), interval=5, logger=events.append)

    def explode(*args, **kwargs):
        worker.stop_event.set()
        raise RuntimeError("tick exploded")

    monkeypatch.setattr(fleet_worker.fleet, "run_tick", explode)
    worker.run()

    assert [e["event"] for e in events] == [
        "fleet_worker_started", "fleet_worker_error",
        "fleet_worker_stopped"]
    assert events[1]["detail"] == "tick exploded"


def test_worker_start_is_idempotent(tmp_path, monkeypatch):
    worker = fleet_worker.FleetWorker(
        {}, registry=Registry(tmp_path), interval=5, logger=lambda e: None)
    monkeypatch.setattr(worker, "run", lambda: worker.stop_event.wait())
    first = worker.start()
    thread = worker.thread
    second = worker.start()
    assert first is second is worker
    assert worker.thread is thread
    worker.stop()


def test_service_spawner_marks_child_as_service_mode(tmp_path, monkeypatch):
    captured = {}

    class Proc:
        pid = 4321

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return Proc()

    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    pid = service.default_spawner(8765, str(tmp_path))

    assert pid == 4321
    assert captured["env"]["AGENTIC_SERVICE_MODE"] == "1"
    assert captured["argv"][-2:] == ["--port", "8765"]
