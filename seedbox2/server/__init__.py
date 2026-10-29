"""Seedbox 2's HTTP API: a FastAPI app on 127.0.0.1 with one router per area and one event stream.

    app = create_app(static_dir)                  # tests: fastapi.testclient.TestClient(app)
    handle = start_server(static_dir, port=0)     # the window and --server-only: a background thread
    handle.url, handle.events.publish("jobs.updated", {...}), handle.stop()

Routers live in seedbox2/server/routers/ (system, library, analysis, sets, mixes, jobs, events), all under /api.
"""

from seedbox2.server.app import create_app
from seedbox2.server.events import EventBus
from seedbox2.server.run import DEFAULT_PORT, ServerHandle, start_server

__all__ = ["DEFAULT_PORT", "EventBus", "ServerHandle", "create_app", "start_server"]
