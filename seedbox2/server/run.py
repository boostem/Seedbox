"""start_server(): the API on a background uvicorn thread, for the window (seedbox2/shell.py) and --server-only."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from seedbox2.server.app import create_app
from seedbox2.server.events import EventBus

DEFAULT_PORT = 8766  # --dev's fixed port, where frontend2's Vite proxies /api (Seedbox 1 uses 8765)


def _uvicorn_server(app: FastAPI, log_level: str, host: str, port: int) -> Any:
    import uvicorn

    class _Server(uvicorn.Server):
        async def shutdown(self, sockets: Any = None) -> None:
            app.state.events.close()  # end event streams first, or graceful shutdown waits on them
            await super().shutdown(sockets=sockets)

    config = uvicorn.Config(app, host=host, port=port, log_level=log_level, timeout_graceful_shutdown=3,
                            lifespan="on", access_log=False, ws="none")  # fmt: skip
    return _Server(config)


@dataclass
class ServerHandle:
    """A server running on a background thread (see start_server)."""

    url: str
    port: int
    app: FastAPI
    _server: Any
    _thread: threading.Thread

    @property
    def events(self) -> EventBus:
        return self.app.state.events

    def stop(self, timeout: float = 5.0) -> None:
        self._server.should_exit = True
        self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread.is_alive()


def start_server(
    static_dir: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    dev: bool = False,
    log_level: str = "warning",
    timeout: float = 15.0,
    library_path: Path | None = None,
    start_jobs: bool = False,
) -> ServerHandle:
    """Start the API (and the built frontend) on a daemon thread; return once it accepts connections.

    `port=0` picks a free port (read it from handle.port / handle.url). `library_path`, `start_jobs`: see create_app.
    """
    app = create_app(static_dir, dev=dev, library_path=library_path, start_jobs=start_jobs)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as e:
        sock.close()
        raise RuntimeError(f"port {port} on {host} is in use ({e})") from e
    sock.listen(128)
    sock.set_inheritable(True)
    real_port = sock.getsockname()[1]
    server = _uvicorn_server(app, log_level, host, real_port)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="seedbox2-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("the Seedbox 2 server failed to start")
        if time.monotonic() > deadline:
            server.should_exit = True
            raise RuntimeError("the Seedbox 2 server didn't start in time")
        time.sleep(0.02)
    return ServerHandle(f"http://{host}:{real_port}", real_port, app, server, thread)
