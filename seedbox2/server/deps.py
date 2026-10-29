"""Shared state the routers reach through `request.app.state`, as FastAPI dependencies."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, Request

from seedbox2.jobs import Supervisor
from seedbox2.library import Library, default_path

_open_lock = threading.RLock()


def get_library(request: Request) -> Library:
    """The app's Library, opened (and migrated) on first use, so starting the server doesn't wait for it."""
    state = request.app.state
    if state.library is None:
        with _open_lock:
            if state.library is None:
                state.library = Library.open(Path(state.library_path) if state.library_path else default_path())
    return state.library


LibraryDep = Annotated[Library, Depends(get_library)]


def get_jobs(request: Request) -> Supervisor:
    """The app's job Supervisor, started (and its jobs recovered) on first use or by create_app(start_jobs=True)."""
    return start_jobs(request.app)


def start_jobs(app: Any) -> Supervisor:
    state = app.state
    if state.jobs is None:
        with _open_lock:
            if state.jobs is None:
                if state.library is None:
                    state.library = Library.open(Path(state.library_path) if state.library_path else default_path())
                state.jobs = Supervisor(state.library, state.events).start()
    return state.jobs


JobsDep = Annotated[Supervisor, Depends(get_jobs)]
