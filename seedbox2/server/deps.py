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


def get_local_music(request: Request) -> Any:
    return local_music(request.app)


def local_music(app: Any) -> Any:
    """The app's LocalMusic (watch folders, imports of files and Rekordbox/iTunes XML), made on first use. New
    tracks get their analyses queued on the job Supervisor. Its settings.json and artwork live next to the
    library file."""
    from seedbox2 import paths
    from seedbox2.jobs import REGISTRY
    from seedbox2.library.importer import queue_analysis
    from seedbox2.library.service import SETTINGS_FILE, LocalMusic, Settings

    state = app.state
    if state.local_music is None:
        with _open_lock:
            if state.local_music is None:
                if state.library is None:
                    state.library = Library.open(Path(state.library_path) if state.library_path else default_path())
                lib = state.library
                data = Path(lib.db.path).parent

                def on_new(hashes: list[str]) -> None:
                    if REGISTRY.per_track():  # no analysis registered yet: don't start the workers for nothing
                        queue_analysis(start_jobs(app), hashes)

                def publish(status: dict) -> None:
                    state.events.publish("library.import", status)

                state.local_music = LocalMusic(lib, settings=Settings(data / SETTINGS_FILE),
                                               music_dir=paths.folders()["music"], artwork_dir=data / "artwork",
                                               on_new=on_new, publish=publish)  # fmt: skip
    return state.local_music


LocalMusicDep = Annotated[Any, Depends(get_local_music)]


def get_models(request: Request) -> Any:
    return models(request.app)


def models(app: Any) -> Any:
    """The app's ModelManager (#42), made on first use: the models in the cache folder, their downloads (events on
    the app's bus) and the `models` settings in settings.json next to the library file."""
    from seedbox2.library.service import SETTINGS_FILE, Settings
    from seedbox2.models.manager import ModelManager

    state = app.state
    if state.models is None:
        with _open_lock:
            if state.models is None:
                folder = Path(state.library_path).parent if state.library_path else default_path().parent
                state.models = ModelManager(state.models_dir, settings=Settings(folder / SETTINGS_FILE),
                                            publish=state.events.publish)  # fmt: skip
    return state.models


ModelsDep = Annotated[Any, Depends(get_models)]
