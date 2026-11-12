"""/library: tracks, crates and imports, from the SQLite library keyed by audio content hash (#38, #40).

Everything goes through `seedbox2.library.Library` (no SQL here).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from starlette.responses import Response

from seedbox2.server.deps import LibraryDep, LocalMusicDep, local_music

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
            if report.new_hashes and local_music(request.app).importer.on_new is not None:
                local_music(request.app).importer.on_new(report.new_hashes)
            update({"state": "done", **report.to_json()})
        except Exception as e:  # noqa: BLE001
            log.exception("Seedbox 1 import failed")
            update({"state": "failed", "error": f"{type(e).__name__}: {e}"})
        finally:
            _import_lock.release()

    update({"state": "running", "done": 0, "total": None, "crate": None})
    threading.Thread(target=run, name="seedbox2-import-seedbox1", daemon=True).start()
    return state.seedbox1_import


# --- local music (#40): watch folders, dropped files and folders, Rekordbox/iTunes XML ----------------------------


class FolderBody(BaseModel):
    path: str


class ImportBody(BaseModel):
    paths: list[str]


@router.get("/folders")
def watch_folders(music: LocalMusicDep) -> dict:
    return {"folders": [{"path": f, "exists": Path(f).is_dir()} for f in music.watch_folders()],
            "scanning": music.watcher.scanning}  # fmt: skip


@router.post("/folders")
def add_watch_folder(body: FolderBody, music: LocalMusicDep) -> dict:
    """Watch a folder: scan it now, then follow its changes."""
    try:
        music.add_watch_folder(body.path)
    except NotADirectoryError:
        raise HTTPException(422, f"not a folder: {body.path}") from None
    return watch_folders(music)


@router.post("/folders/remove")
def remove_watch_folder(body: FolderBody, music: LocalMusicDep) -> dict:
    """Stop watching a folder; its tracks stay in the library."""
    try:
        music.remove_watch_folder(body.path)
    except KeyError:
        raise HTTPException(404, f"not a watch folder: {body.path}") from None
    return watch_folders(music)


@router.get("/import")
def import_status(music: LocalMusicDep) -> dict:
    return music.status


@router.post("/import", status_code=202)
def import_files(body: ImportBody, music: LocalMusicDep) -> dict:
    """Import files and folders (what was dropped onto the window). Progress: `library.import` events."""
    missing = [p for p in body.paths if not Path(p).expanduser().exists()]
    if not body.paths or missing:
        raise HTTPException(422, f"not found: {', '.join(missing)}" if missing else "no paths")
    return music.import_files(body.paths)


@router.post("/import/xml", status_code=202)
def import_xml(body: FolderBody, music: LocalMusicDep) -> dict:
    """Import the playlists of a Rekordbox XML or an iTunes/Music library XML as crates."""
    from seedbox2.library.playlists_xml import NotAPlaylistFile

    try:
        return music.import_xml(body.path)
    except (NotAPlaylistFile, OSError) as e:
        raise HTTPException(422, str(e)) from None


@router.post("/upload", status_code=202)
async def upload(request: Request, name: str, music: LocalMusicDep) -> dict:
    """A file dropped onto the app in a browser (which sends the bytes, not the path) as the raw body: saved into
    the music folder's Imported folder, then imported."""
    from seedbox2.library.tags import is_audio

    if not is_audio(name):
        raise HTTPException(415, f"not an audio file Seedbox 2 reads: {name}")
    dest = music.upload_path(name)
    part = dest.with_name(f".{dest.name}.part")  # hidden: the watch folders ignore it until it is complete
    import anyio

    try:
        async with await anyio.open_file(part, "wb") as f:
            async for chunk in request.stream():
                await f.write(chunk)
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return music.import_files([dest])


class ChooseBody(BaseModel):
    kind: str = "folder"  # folder, or xml (a Rekordbox/iTunes library file)


@router.post("/choose")
def choose(body: ChooseBody) -> dict:
    """Ask for a folder or an XML file with the OS's dialog (in the app's window; 501 in a browser)."""
    import sys

    webview = sys.modules.get("webview")
    windows = getattr(webview, "windows", None) if webview is not None else None
    if not windows:
        raise HTTPException(501, "no window to show a dialog in: type the path instead")
    if body.kind == "folder":
        picked = windows[0].create_file_dialog(webview.FileDialog.FOLDER)  # type: ignore[union-attr]
    else:
        picked = windows[0].create_file_dialog(webview.FileDialog.OPEN,  # type: ignore[union-attr]
                                               file_types=("Library XML (*.xml)", "All files (*.*)"))  # fmt: skip
    return {"paths": [str(p) for p in picked or []]}


# --- the library view (#73): table rows, overview waveforms, crates and sets to drop tracks into, smart crates -----


def _stems_required(lib: Any) -> bool:
    """Whether a ready track needs its stems: not when stems are off in the settings."""
    from seedbox2.analysis import stems
    from seedbox2.library.service import SETTINGS_FILE, Settings

    return stems.read_settings(Settings(Path(lib.db.path).parent / SETTINGS_FILE))["enabled"]


@router.get("/view")
def view(lib: LibraryDep) -> dict:
    """Every track as a table row: BPM, Camelot key, energy and genre (our analysis, else the tags' hints) and its
    analysis status (`seedbox2.library.view`). The table sorts and filters these on the client."""
    from seedbox2.analysis import waveform

    items = lib.view_rows(waveform_version=waveform.MODEL_VERSION, stems_required=_stems_required(lib))
    return {"total": len(items), "items": items}


class HashesBody(BaseModel):
    hashes: list[str]


@router.post("/view")
def view_some(body: HashesBody, lib: LibraryDep) -> dict:
    """The rows of these tracks only (the table refreshes the ones whose jobs just finished); unknown ones are left
    out."""
    from seedbox2.analysis import waveform

    items = lib.view_rows(waveform_version=waveform.MODEL_VERSION, hashes=body.hashes,
                          stems_required=_stems_required(lib))
    return {"total": len(items), "items": items}


_overviews = None


@router.post("/overviews", response_class=Response, responses={200: {"content": {"application/octet-stream": {}}}})
def overviews(body: HashesBody, lib: LibraryDep) -> Response:
    """The 1,200-column peak overviews (low, mid, high) of up to 500 tracks in one binary response
    (`seedbox2.library.view.pack_overviews`); a track without a waveform yet is marked absent."""
    global _overviews
    from seedbox2.analysis import waveform
    from seedbox2.library import view

    if len(body.hashes) > 500:
        raise HTTPException(422, "at most 500 tracks at a time")
    if _overviews is None:
        _overviews = view.OverviewCache()
    rows = []
    for h in body.hashes:
        path = waveform.current_artifact(lib, h)
        rows.append(_overviews.get(path) if path is not None else None)
    data = view.pack_overviews(rows, columns=waveform.OVERVIEW_COLUMNS, bands=len(waveform.BANDS))
    return Response(data, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})


class NewCrate(BaseModel):
    name: str
    hashes: list[str] = []


@router.post("/crates", status_code=201)
def new_crate(body: NewCrate, lib: LibraryDep) -> dict:
    if not body.name.strip():
        raise HTTPException(422, "a crate needs a name")
    crate_id = lib.save_crate(body.name.strip(), [])
    _add(lib.add_to_crate, crate_id, body.hashes)
    return lib.crate(crate_id)  # type: ignore[return-value]


@router.post("/crates/{crate_id}/tracks")
def add_to_crate(crate_id: int, body: HashesBody, lib: LibraryDep) -> dict:
    """Append tracks to a crate (dragged onto it); tracks already in it stay where they are."""
    return {"added": _add(lib.add_to_crate, crate_id, body.hashes), "hashes": lib.crate_hashes(crate_id)}


@router.get("/crates/{crate_id}/hashes")
def crate_hashes(crate_id: int, lib: LibraryDep) -> dict:
    hashes = lib.crate_hashes(crate_id)
    if hashes is None:
        raise HTTPException(404, "no such crate")
    return {"hashes": hashes}


def _add(fn, target_id: int, hashes: list[str]) -> int:  # noqa: ANN001
    try:
        return fn(target_id, hashes)
    except KeyError:
        raise HTTPException(404, "no such crate or set") from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


class SmartCrates(BaseModel):
    crates: list[dict]


@router.get("/smart-crates")
def smart_crates(music: LocalMusicDep) -> dict:
    """Saved filters (`{"id", "name", "filter"}`; the filter's fields are the library page's), kept in settings.json."""
    crates = music.settings.get("smart_crates")
    return {"crates": crates if isinstance(crates, list) else []}


@router.put("/smart-crates")
def save_smart_crates(body: SmartCrates, music: LocalMusicDep) -> dict:
    if any(not isinstance(c.get("name"), str) or not isinstance(c.get("filter"), dict) for c in body.crates):
        raise HTTPException(422, "each smart crate needs a name and a filter")
    music.settings.set("smart_crates", body.crates)
    return {"crates": body.crates}
