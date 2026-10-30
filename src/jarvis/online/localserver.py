"""A Jarvis Online server that runs on THIS PC only, started with ``jarvis local-server`` (or its Start-menu entry).

It is the same stdlib server as ``server/jarvis_server`` (the one a real deployment would use), bound to 127.0.0.1 and nothing else: the address is not a
setting, so it cannot be exposed to a network from here. Its database is ``<data folder>/online/local_server.db``. Telemetry intake is off. It is optional:
the local AI, memory, voice, missions and desktop control never need it. Accounts made on it exist only on this PC, so they are not accounts on any public
service (none exists). It does not connect the local model to the server."""
from __future__ import annotations

import sys
from pathlib import Path

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _import_server():
    try:
        import jarvis_server.__main__ as m
        return m
    except ImportError:
        here = Path(__file__).resolve()
        for up in range(2, 6):              # a source checkout keeps the server next to src/
            cand = here.parents[up] / "server"
            if (cand / "jarvis_server").is_dir():
                sys.path.insert(0, str(cand))
                import jarvis_server.__main__ as m
                return m
        raise


def run_local_server(cfg, port: int = DEFAULT_PORT) -> int:
    if not 1024 <= port <= 65535:
        print("The port must be a number from 1024 to 65535.", file=sys.stderr)
        return 2
    try:
        server_main = _import_server()
    except ImportError:
        print("The sign-in server code is not part of this copy of Jarvis.", file=sys.stderr)
        return 3
    db_dir = Path(cfg.data_dir) / "online"
    db_dir.mkdir(parents=True, exist_ok=True)
    print(f"Local Jarvis sign-in server for THIS PC only. In the window open Account, and use http://{HOST}:{port}", flush=True)
    print("Leave this window open while you use it. Press Ctrl+C to stop.", flush=True)
    try:
        return int(server_main.main(["--db", str(db_dir / "local_server.db"), "--host", HOST, "--port", str(port), "--no-telemetry"]))
    except OSError as e:
        print(f"Could not start on port {port}: {e.strerror or e}. Another program may be using it; try --port {port + 1}.", file=sys.stderr)
        return 4
