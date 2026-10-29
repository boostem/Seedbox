"""/library: tracks, crates and imports, from the SQLite library keyed by audio content hash (#38, #40).

Everything goes through `seedbox2.library.Library` (no SQL here).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from seedbox2.server.deps import LibraryDep

log = logging.getLogger(__name__)

router = APIRouter(prefix="/library", tags=["library"])


@router.get("")
def summary(lib: LibraryDep) -> dict:
    return lib.stats()


@router.get("/tracks")
def tracks(lib: LibraryDep, search: str = "", source: str | None = None, limit: int = Query(100, ge=1, le=1000),
           offset: int = Query(0, ge=0)) -> dict:  # fmt: skip
    return {"total": lib.track_count(), "items": lib.tracks(search=search, source=source, limit=limit, offset=offset)}


@router.get("/tracks/{hash_}")
def track(hash_: str, lib: LibraryDep) -> dict:
    t = lib.track(hash_)
    if t is None:
        raise HTTPException(404, "no such track")
    return {**t, "analyses": lib.analyses(hash_), "artifacts": lib.artifacts(hash_)}


@router.get("/crates")
def crates(lib: LibraryDep) -> list[dict]:
    return lib.crates()


@router.get("/crates/{crate_id}")
def crate(crate_id: int, lib: LibraryDep) -> dict:
    c = lib.crate(crate_id)
    if c is None:
        raise HTTPException(404, "no such crate")
    return c


class Seedbox1Import(BaseModel):
    root: str | None = None  # the crates folder; default: Seedbox 1's (Settings.output_root)


_import_lock = threading.Lock()


@router.get("/import/seedbox1")
def seedbox1_import_status(request: Request) -> dict:
    return getattr(request.app.state, "seedbox1_import", None) or {"state": "idle"}


@router.post("/import/seedbox1", status_code=202)
def seedbox1_import(body: Seedbox1Import, request: Request, lib: LibraryDep) -> dict:
    """Start importing Seedbox 1's crates, sets and edited plans in the background. Progress is published as
    `library.import` events and kept for GET; 409 while one runs."""
    state, bus = request.app.state, request.app.state.events
    root = Path(body.root).expanduser() if body.root else None
    if root is not None and not root.is_dir():
        raise HTTPException(422, f"not a folder: {root}")
    if not _import_lock.acquire(blocking=False):
        raise HTTPException(409, "an import is already running")

    def update(status: dict) -> None:
        state.seedbox1_import = status
        bus.publish("library.import", status)

    def progress(done: int, total: int, folder: str) -> None:
        update({"state": "running", "done": done, "total": total, "crate": folder})

    def run() -> None:
        try:
            from seedbox2.library.import_seedbox1 import import_seedbox1  # musicreco.library: only when used

            report = import_seedbox1(lib, root, progress)
            update({"state": "done", **report.to_json()})
        except Exception as e:  # noqa: BLE001
            log.exception("Seedbox 1 import failed")
            update({"state": "failed", "error": f"{type(e).__name__}: {e}"})
        finally:
            _import_lock.release()

    update({"state": "running", "done": 0, "total": None, "crate": None})
    threading.Thread(target=run, name="seedbox2-import-seedbox1", daemon=True).start()
    return state.seedbox1_import
