"""Background fleet worker for the localhost Agentic OS service.

The dashboard stays responsive while this daemon thread runs one existing
fleet tick at a time.  Scheduling, slot ownership, leases, cooling and cycle
execution remain owned by core.fleet/core.project; this module only supplies
the previously-missing periodic trigger.
"""
import json
import threading

from . import fleet
from .registry import ProjectRegistry


DEFAULT_TICK_SECONDS = 30
MINIMUM_TICK_SECONDS = 5


class FleetWorker:
    def __init__(self, cfg, registry=None, runner=None, interval=None,
                 logger=None):
        self.cfg = cfg
        self.registry = registry or ProjectRegistry()
        self.runner = runner
        configured = ((cfg.get("service") or {})
                      .get("fleet_tick_seconds", DEFAULT_TICK_SECONDS))
        self.interval = max(MINIMUM_TICK_SECONDS,
                            float(interval if interval is not None
                                  else configured))
        self.logger = logger or _default_logger
        self.stop_event = threading.Event()
        self.thread = None

    def tick(self):
        result = fleet.run_tick(
            self.cfg, self.registry, runner=self.runner,
            home=self.registry.home)
        if result.get("start") or result.get("results"):
            self.logger({
                "event": "fleet_tick",
                "start": result.get("start") or [],
                "results": result.get("results") or {},
                "waiting": result.get("waiting") or [],
            })
        return result

    def run(self):
        self.logger({"event": "fleet_worker_started",
                     "interval_seconds": self.interval})
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 -- worker must survive
                self.logger({"event": "fleet_worker_error",
                             "detail": str(exc)[:500]})
            self.stop_event.wait(self.interval)
        self.logger({"event": "fleet_worker_stopped"})

    def start(self):
        if self.thread and self.thread.is_alive():
            return self
        self.thread = threading.Thread(
            target=self.run, name="agentic-fleet-worker", daemon=True)
        self.thread.start()
        return self

    def stop(self, timeout=2):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=timeout)


def _default_logger(event):
    print(json.dumps(event, default=str), flush=True)


def start_worker(cfg, **kwargs):
    return FleetWorker(cfg, **kwargs).start()
