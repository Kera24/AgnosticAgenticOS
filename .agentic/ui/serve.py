"""Start the Control Centre: loopback binding, safe port selection, clear
URL output, optional browser open, graceful Ctrl+C shutdown.

The service is never exposed beyond loopback: a non-loopback host is
rejected outright because no authentication layer exists (by design)."""
import os
import socket
import sys
import webbrowser

from core import config as config_mod

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DEFAULT_PORT = 8765
PORT_SCAN_RANGE = 20


class UIStartupError(Exception):
    pass


def pick_port(host, preferred, scan=PORT_SCAN_RANGE):
    for offset in range(scan + 1):
        port = preferred + offset
        if port > 65535:
            break
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise UIStartupError("no free port found in %d..%d"
                         % (preferred, preferred + scan))


def frontend_dist(cfg):
    root = str(config_mod.repo_root(cfg))
    return os.path.join(root, "ui", "dist")


def run_ui(cfg, port=None, open_browser=None, dev_origin=False,
           host=None):
    ui_cfg = cfg.get("ui") or {}
    host = str(host or ui_cfg.get("host") or "127.0.0.1")
    if host not in LOOPBACK:
        print("refusing to bind %r: the dashboard is loopback-only and has "
              "no remote authentication layer. Use 127.0.0.1." % host,
              file=sys.stderr)
        return 2
    preferred = int(port or ui_cfg.get("port", DEFAULT_PORT))
    try:
        port = pick_port("127.0.0.1", preferred)
    except UIStartupError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    static = frontend_dist(cfg)
    has_frontend = os.path.isfile(os.path.join(static, "index.html"))
    if not has_frontend:
        print("note: built frontend not found at %s\n"
              "      run `npm install && npm run build` inside ui\\ first, "
              "or use the dev server (see README).\n"
              "      serving the API only." % static, file=sys.stderr)

    from ui.app import create_app
    app = create_app(static_dir=static if has_frontend else None,
                     allow_dev_origin=dev_origin
                     or os.environ.get("AGENTIC_UI_DEV") == "1")

    url = "http://127.0.0.1:%d" % port
    print("\nAgentic OS Control Centre")
    print("  dashboard: %s" % url)
    print("  api:       %s/api/v1/health" % url)
    print("  bound to loopback only; Ctrl+C stops the server\n")

    wants_browser = ui_cfg.get("open_browser", True) \
        if open_browser is None else open_browser
    if wants_browser and has_frontend:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    print("dashboard stopped.")
    return 0
