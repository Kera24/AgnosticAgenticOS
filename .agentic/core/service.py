"""Local service lifecycle: `agentic start|stop|restart|status|logs`.

One command starts everything: single-instance lock, detached dashboard
service bound to 127.0.0.1, health wait, browser open (optional), state
recovery (registry + scheduler state are already persisted; eligible
projects resume via the fleet), structured logs, and honest errors.

State in the runtime home:
    service.json     pid / port / url / started_at
    logs/service.log stdout+stderr of the detached service

Everything is injectable (spawner, health probe, browser opener,
terminator, port prober) so tests never spawn processes or open sockets.
"""
import datetime as _dt
import json
import os
import socket
import subprocess
import sys
import time

from . import config as config_mod
from .registry import runtime_home

DEFAULT_PORT = 8765
HEALTH_TIMEOUT_SECONDS = 30


def _paths(home=None):
    home = home or runtime_home()
    return {"home": home,
            "state": os.path.join(home, "service.json"),
            "logs": os.path.join(home, "logs"),
            "log_file": os.path.join(home, "logs", "service.log")}


def read_state(home=None):
    paths = _paths(home)
    if not os.path.exists(paths["state"]):
        return None
    try:
        with open(paths["state"], encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return None


def _write_state(state, home=None):
    paths = _paths(home)
    os.makedirs(paths["home"], exist_ok=True)
    tmp = paths["state"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, paths["state"])


def _clear_state(home=None):
    try:
        os.remove(_paths(home)["state"])
    except OSError:
        pass


def _pid_alive(pid):
    from .fleet import _pid_alive as alive
    return alive(pid)


def _port_free(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(preferred=None, prober=None):
    prober = prober or _port_free
    base = int(preferred or DEFAULT_PORT)
    for offset in range(20):
        port = base + offset
        if prober(port):
            return port
    raise RuntimeError("no free port in %d..%d" % (base, base + 20))


def default_health(port, timeout=3):
    """GET the loopback health endpoint. Lifecycle-only network use."""
    import urllib.request
    url = "http://127.0.0.1:%d/api/v1/health" % port
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:   # noqa: BLE001 — any failure is "not healthy yet"
        return None


def default_spawner(port, home):
    """Detached dashboard process; stdout/stderr into the service log."""
    paths = _paths(home)
    os.makedirs(paths["logs"], exist_ok=True)
    log = open(paths["log_file"], "a", encoding="utf-8")
    log.write("\n[%s] agentic start (port %d)\n"
              % (_dt.datetime.now().isoformat(timespec="seconds"), port))
    log.flush()
    argv = [sys.executable, str(config_mod.AGENTIC_DIR / "run"), "ui",
            "--no-open", "--port", str(port)]
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | \
            subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(argv, stdout=log, stderr=log,
                            cwd=str(config_mod.AGENTIC_DIR.parent),
                            creationflags=flags)
    return proc.pid


def default_terminator(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def default_opener(url):
    import webbrowser
    webbrowser.open(url)


def running_service(home=None, health=None):
    """The live service state, or None. A dead pid or failed health check
    is reported as not running (and the stale state cleared)."""
    health = health or default_health
    state = read_state(home)
    if not state:
        return None
    if not _pid_alive(state.get("pid")):
        _clear_state(home)
        return None
    if health(state.get("port", DEFAULT_PORT)) is None:
        return dict(state, healthy=False)
    return dict(state, healthy=True)


def start(cfg, port=None, no_open=False, paused=False, project=None,
          home=None, spawner=None, health=None, opener=None, prober=None,
          wait_seconds=HEALTH_TIMEOUT_SECONDS, poll_interval=0.5):
    """Start (or attach to) the local service. Returns a report dict —
    never leaves a half-started state behind."""
    home = home or runtime_home()
    spawner = spawner or default_spawner
    health = health or default_health
    opener = opener or default_opener

    existing = running_service(home, health=health)
    if existing and existing.get("healthy"):
        result = dict(existing, status="already_running")
        if not no_open:
            _try_open(opener, existing["url"], result)
        return result

    chosen_port = pick_port(port or (cfg.get("ui") or {}).get("port"),
                            prober=prober)
    pid = spawner(chosen_port, home)
    url = "http://127.0.0.1:%d" % chosen_port
    deadline = time.time() + wait_seconds
    healthy = None
    while time.time() < deadline:
        healthy = health(chosen_port)
        if healthy:
            break
        if not _pid_alive(pid):
            return {"status": "failed", "pid": pid,
                    "detail": "service process exited during startup; "
                              "see %s" % _paths(home)["log_file"]}
        time.sleep(poll_interval)
    if not healthy:
        return {"status": "failed", "pid": pid,
                "detail": "health check did not pass within %ss; see %s"
                          % (wait_seconds, _paths(home)["log_file"])}

    state = {"pid": pid, "port": chosen_port, "url": url,
             "started_at": _dt.datetime.now().isoformat(
                 timespec="seconds")}
    _write_state(state, home)

    if paused:
        from .fleet import set_global_pause
        set_global_pause(home, True)
    if project:
        from .registry import ProjectRegistry, RegistryError
        try:
            ProjectRegistry(home=home).update(project, enabled=True)
        except RegistryError as exc:
            state["project_warning"] = str(
                exc.detail if hasattr(exc, "detail") else exc)

    result = dict(state, status="started", paused=paused,
                  log_file=_paths(home)["log_file"])
    if not no_open:
        _try_open(opener, url, result)
    return result


def _try_open(opener, url, result):
    try:
        opener(url)
        result["browser_opened"] = True
    except Exception as exc:   # noqa: BLE001 — browser failure never fatal
        result["browser_opened"] = False
        result["browser_error"] = str(exc)[:100]
        result["note"] = "open %s manually" % url


def default_shutdowner(port, timeout=3):
    """Graceful stop via the protected loopback shutdown endpoint."""
    import urllib.request
    request = urllib.request.Request(
        "http://127.0.0.1:%d/api/v1/shutdown" % port,
        data=json.dumps({"confirm": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except Exception:   # noqa: BLE001
        return False


def stop(home=None, terminator=None, health=None, shutdowner=None):
    """Graceful shutdown first (loopback endpoint), hard terminate as the
    fallback."""
    home = home or runtime_home()
    terminator = terminator or default_terminator
    shutdowner = shutdowner or default_shutdowner
    state = read_state(home)
    if not state or not _pid_alive(state.get("pid")):
        _clear_state(home)
        return {"status": "not_running"}
    graceful = shutdowner(state.get("port", DEFAULT_PORT))
    for _ in range(10 if graceful else 0):
        if not _pid_alive(state["pid"]):
            break
        time.sleep(0.2)
    forced = False
    if _pid_alive(state["pid"]):
        terminator(state["pid"])
        forced = True
        for _ in range(20):
            if not _pid_alive(state["pid"]):
                break
            time.sleep(0.2)
    _clear_state(home)
    return {"status": "stopped", "pid": state["pid"],
            "graceful": graceful and not forced}


def restart(cfg, home=None, **kw):
    stop_result = stop(home=home, terminator=kw.pop("terminator", None))
    result = start(cfg, home=home, **kw)
    result["previous"] = stop_result["status"]
    return result


def status(cfg, home=None, health=None):
    home = home or runtime_home()
    service = running_service(home, health=health)
    from .fleet import SlotManager, load_fleet_state
    report = {
        "service": service or {"status": "not_running"},
        "runtime_home": home,
        "global_pause": load_fleet_state(home)["global_pause"],
        "slots": SlotManager(home).usage(),
        "log_file": _paths(home)["log_file"],
    }
    try:
        from .registry import ProjectRegistry
        report["projects"] = len(ProjectRegistry(home=home)
                                 .list(include_archived=False))
    except Exception:   # noqa: BLE001
        report["projects"] = None
    return report


def logs(home=None, lines=50):
    path = _paths(home)["log_file"]
    if not os.path.exists(path):
        return {"log_file": path, "lines": []}
    with open(path, encoding="utf-8", errors="replace") as fh:
        tail = fh.readlines()[-int(lines):]
    return {"log_file": path, "lines": [line.rstrip("\n")
                                        for line in tail]}
