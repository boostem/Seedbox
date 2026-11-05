"""Shared state the routers reach through `request.app.state`, as FastAPI dependencies."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, Request

from seedbox2.jobs import Supervisor
from seedbox2.library import Library, default_path

log = logging.getLogger(__name__)
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
                from seedbox2.analysis import energy, key, stems, vibe, vocals

                lib, events = state.library, state.events
                jobs = Supervisor(lib, events)
                jobs.add_listener(lambda row: stems.on_job_finished(jobs, lib, events, row))
                jobs.add_listener(lambda row: key.on_job_finished(jobs, lib, row))
                jobs.add_listener(lambda row: energy.on_job_finished(jobs, lib, row))
                jobs.add_listener(lambda row: vibe.on_job_finished(jobs, lib, row))
                jobs.add_listener(lambda row: vocals.on_job_finished(jobs, lib, row))
                monitor = power(app)
                monitor.attach(jobs)  # before it starts: jobs left from the last run obey the limits too
                monitor.start()
                state.jobs = jobs.start()
                threading.Thread(target=_queue_missing_stems, args=(app,), name="seedbox2-stems-queue",
                                 daemon=True).start()  # fmt: skip
    return state.jobs


def _queue_missing_stems(app: Any) -> None:
    """The stems of tracks imported before the model was there, or left out when the cache was full, and the keys,
    energy, embeddings and vocals of tracks imported before those analyses."""
    from seedbox2.analysis import energy, key, stems, vibe, vocals
    from seedbox2.library.service import SETTINGS_FILE, Settings

    state = app.state
    try:
        stems.queue_missing(state.jobs, state.library, Settings(Path(state.library.db.path).parent / SETTINGS_FILE))
    except Exception:
        log.exception("couldn't queue the missing stems")
    try:
        key.queue_missing(state.jobs, state.library)
    except Exception:
        log.exception("couldn't queue the missing keys")
    try:
        energy.queue_missing(state.jobs, state.library)
        vibe.queue_missing(state.jobs, state.library)
    except Exception:
        log.exception("couldn't queue the missing energy and embeddings")
    try:
        vocals.queue_missing(state.jobs, state.library)
    except Exception:
        log.exception("couldn't queue the missing vocals")


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

                def publish(type_: str, data: Any = None, **kw: Any) -> None:
                    state.events.publish(type_, data, **kw)
                    if type_ == "model.done" and isinstance(data, dict) and data.get("name") in ("htdemucs", "htdemucs_int8"):
                        threading.Thread(target=lambda: (start_jobs(app), _queue_missing_stems(app)),
                                         name="seedbox2-stems-queue", daemon=True).start()  # fmt: skip

                state.models = ModelManager(state.models_dir, settings=Settings(folder / SETTINGS_FILE),
                                            publish=publish)  # fmt: skip
    return state.models


ModelsDep = Annotated[Any, Depends(get_models)]


def get_power(request: Request) -> Any:
    return power(request.app)


def power(app: Any) -> Any:
    """The app's PowerMonitor (#85), made on first use, with its `power` setting next to the library file. It
    polls once the jobs have started, and it limits them."""
    from seedbox2.library.service import SETTINGS_FILE, Settings
    from seedbox2.power import PowerMonitor

    state = app.state
    if state.power is None:
        with _open_lock:
            if state.power is None:
                folder = Path(state.library_path).parent if state.library_path else default_path().parent
                state.power = PowerMonitor(Settings(folder / SETTINGS_FILE), publish=state.events.publish,
                                           forced_mode=state.power_mode)  # fmt: skip
    return state.power


PowerDep = Annotated[Any, Depends(get_power)]


def onboarding_pending(app: Any) -> bool:
    """Is this a first run (#80)? Setup was never finished and the library is empty: someone who already has tracks
    has no use for it. Until it is over, models don't download on their own, so its "skip" means skip."""
    if models(app).settings.get("onboarded"):
        return False
    state = app.state
    if state.library is None:
        with _open_lock:
            if state.library is None:
                state.library = Library.open(Path(state.library_path) if state.library_path else default_path())
    return state.library.track_count() == 0


def get_tunebat(request: Request) -> Any:
    """The first run's tunebat search and one-track download, made on first use."""
    from seedbox2.library.tunebat_try import TunebatTry

    state = request.app.state
    if state.tunebat is None:
        with _open_lock:
            if state.tunebat is None:
                state.tunebat = TunebatTry(local_music(request.app), state.events.publish)
    return state.tunebat


TunebatDep = Annotated[Any, Depends(get_tunebat)]
