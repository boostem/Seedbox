"""Shared state the routers reach through `request.app.state`, as FastAPI dependencies."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from seedbox2.library import Library, default_path

_open_lock = threading.Lock()


def get_library(request: Request) -> Library:
    """The app's Library, opened (and migrated) on first use, so starting the server doesn't wait for it."""
    state = request.app.state
    if state.library is None:
        with _open_lock:
            if state.library is None:
                state.library = Library.open(Path(state.library_path) if state.library_path else default_path())
    return state.library


LibraryDep = Annotated[Library, Depends(get_library)]
