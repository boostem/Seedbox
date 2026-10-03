"""Seedbox HTTP API (FastAPI) around the musicreco engine; JSON shapes per frontend/src/api/types.ts.

Threading model: every endpoint except the SSE stream is a plain `def`, so FastAPI runs it in its worker
threadpool and the event loop never blocks on disk, tunebat or tag I/O. Long work runs on dedicated threads:
one per download job (jobs.py), 1-2 analysis workers (analysis_queue.py) and the PreviewManager's own pool.
Those threads report through the thread-safe EventBus, which hands events to each SSE client's asyncio queue.

Run:  python -m seedbox.server --port 8765 --dev [--static frontend/dist]
Embed (app.py):  handle = start_server(settings, static_dir); set_folder_picker(fn); ...; handle.stop()
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib
import logging
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, Response, StreamingResponse

from . import updater

updater.activate()  # before anything imports yt_dlp (`python -m seedbox.server`); idempotent after app.py

from musicreco import library  # noqa: E402
from musicreco.models import Track  # noqa: E402
from musicreco.tunebat import TunebatError  # noqa: E402

from . import engine, serializers
from .analysis_queue import AnalysisQueue
from .crates import ApiError, CrateService, NotFound, inside, resolve_crate_file, track_json
from .events import HEARTBEAT_S, EventBus
from .jobs import Job, JobManager
from .mixes import MixService
from .serializers import JSON
from .setlists import SetlistService
from .setmixes import SetMixService
from .stems_queue import StemQueue
from .tunebat_service import TunebatService

log = logging.getLogger(__name__)

DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
_TRACK_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")


def app_version() -> str:
    import seedbox

    return str(getattr(seedbox, "__version__", None) or "0.1.0")


def _updater() -> Any | None:
    try:
        return importlib.import_module("seedbox.updater")
    except ImportError:
        return None


def ytdlp_fields() -> dict:
    """SystemStatus ytdlp_version / ytdlp_latest (the updater checks PyPI in the background, never blocking)."""
    upd = _updater()
    if upd is not None and hasattr(upd, "system_fields"):
        try:
            return dict(upd.system_fields())
        except Exception:  # noqa: BLE001
            log.exception("updater.system_fields failed")
    try:
        from yt_dlp.version import __version__ as version
    except Exception:  # noqa: BLE001
        version = "unknown"
    return {"ytdlp_version": version, "ytdlp_latest": None}


# Native folder dialog, registered by seedbox/app.py once its window exists (none in a browser / dev server).
FolderPicker = Callable[[str | None], "str | Path | list | tuple | None"]
_folder_picker: FolderPicker | None = None
_picker_busy = threading.Lock()


def set_folder_picker(picker: FolderPicker | None) -> None:
    """Register (None: clear) the native folder dialog behind POST /api/system/pick-folder.

    `picker(initial_dir)` runs on a server worker thread and may block until the user answers. It returns the
    chosen folder (str or Path; for a list/tuple the first entry is used) or None/empty when cancelled.
    """
    global _folder_picker
    _folder_picker = picker


def open_in_finder(path: Path) -> None:
    """Reveal `path` in Finder (non-blocking)."""
    subprocess.Popen(["open", "-R", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- request bodies -----------------------------------------------------------------------------------------------


class JobRequest(BaseModel):
    seed_id: str
    track_ids: list[str]


class OrderRequest(BaseModel):
    track_ids: list[str]


class RevealRequest(BaseModel):
    path: str


# --- app state ----------------------------------------------------------------------------------------------------


class AppState:
    """Everything the endpoints share. One per app."""

    def __init__(
        self,
        settings: Any,
        cache_dir: Path | None,
        save_settings: Callable[[Any], None] | None,
        tunebat: TunebatService | None,
        heartbeat_s: float,
        analysis_workers: int | None,
    ) -> None:
        self.settings = settings
        self._cache_dir = cache_dir
        self.save_settings = save_settings or engine.save_settings
        self.bus = EventBus(heartbeat_s=heartbeat_s)
        self.tunebat = tunebat or TunebatService()
        self.analysis = AnalysisQueue(self.bus, workers=analysis_workers)
        self.jobs = JobManager(self.bus, lambda: self.settings, on_finished=self._job_finished)
        self.crates = CrateService(lambda: self.settings, self.analysis, self.bus, busy=self.jobs.busy)
        self.mixes = MixService(self.crates, self.bus, lambda: self.cache_dir)
        self.setlists = SetlistService(self.crates, self.mixes, self.bus)
        self.setmixes = SetMixService(self.crates, self.mixes, self.setlists, self.bus)
        # vocal splitting: songs in use first; the rest of a crate only while nothing renders
        self.stems = StemQueue(self.bus, idle=lambda: not (self.mixes.busy() or self.setmixes.busy()))
        self.crates.stems = self.stems
        self._lock = threading.RLock()
        self._previews: Any = None
        self._last_preload: list[Track] = []
        # Every `preview` event goes through this one thread, in order: the manager's own status changes and the
        # snapshots sent when a crate opens. A snapshot is read when its turn comes, so it can never overwrite a
        # newer state that the manager reported before it.
        self._preview_q: queue.SimpleQueue = queue.SimpleQueue()
        threading.Thread(target=self._preview_events, name="seedbox-preview-events", daemon=True).start()

    @property
    def root(self) -> Path:
        return Path(self.settings.output_root)

    @property
    def cache_dir(self) -> Path:
        if self._cache_dir is None:
            self._cache_dir = engine.cache_dir()
        return self._cache_dir

    # previews

    def previews(self, required: bool = True) -> Any:
        """The PreviewManager (created on first use); None/503 when musicreco.previews isn't available."""
        with self._lock:
            if self._previews is None:
                mod = engine.optional("previews")
                if mod is None:
                    if required:
                        raise ApiError("previews aren't available in this build", 503)
                    return None
                self._previews = mod.PreviewManager(**self._preview_kwargs(mod))
            return self._previews

    def _preview_kwargs(self, mod: Any) -> dict:
        s = self.settings
        kw: dict[str, Any] = {
            "cache_dir": self.cache_dir,
            "concurrency": int(s.preview_concurrency),
            "max_cache_mb": int(s.preview_cache_mb),
            "on_event": lambda status: self._preview_q.put(("event", status)),
        }
        if engine.accepts(mod.PreviewManager, "tolerance_s"):
            kw["tolerance_s"] = float(s.tolerance_s)
        return kw

    def preload(self, tracks: list[Track]) -> None:
        mgr = self.previews(required=False)
        if mgr is None:
            return
        with self._lock:
            self._last_preload = list(tracks)
        mgr.clear_queue()
        mgr.preload(tracks)
        # Clips already cached (ready / no_match) or in flight get no event from preload: send everyone's status.
        self._preview_q.put(("snapshot", (mgr, [t.id for t in tracks])))

    def _preview_events(self) -> None:
        while (item := self._preview_q.get()) is not None:
            kind, payload = item
            try:
                if kind == "event":
                    self.bus.publish("preview", payload)
                else:
                    mgr, ids = payload
                    for tid in dict.fromkeys(ids):
                        self.bus.publish("preview", mgr.status(tid))
            except Exception:  # noqa: BLE001 - never kill the event thread
                log.exception("preview event failed")

    # settings

    def update_settings(self, patch: dict) -> Any:
        if not isinstance(patch, dict):
            raise ApiError("settings must be a JSON object", 422)
        with self._lock:
            old = self.settings
            merged = {**engine.settings_to_json(old), **patch}
            try:
                new = engine.settings_from_json(merged, strict=True)
            except ValueError as e:
                raise ApiError(str(e), 422) from e
            if Path(new.output_root) != Path(old.output_root):
                try:
                    Path(new.output_root).mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    raise ApiError(f"can't use {new.output_root} as the crates folder: {e}") from e
            self.save_settings(new)
            self.settings = new
            if Path(new.output_root) != Path(old.output_root):
                self.analysis.forget()
                self.stems.forget()
            self._apply_preview_settings(old, new)
        return new

    def _apply_preview_settings(self, old: Any, new: Any) -> None:
        mgr = self._previews
        if mgr is None:
            return
        if int(new.preview_concurrency) != int(old.preview_concurrency):
            mgr.shutdown()
            self._previews = None
            if self._last_preload:
                self.previews(required=False).preload(self._last_preload)
            return
        if hasattr(mgr, "max_cache_bytes"):
            mgr.max_cache_bytes = max(0, int(new.preview_cache_mb)) * 1024 * 1024
        if hasattr(mgr, "tolerance_s"):
            mgr.tolerance_s = float(new.tolerance_s)

    # jobs

    def _job_finished(self, job: Job) -> None:
        self.crates.invalidate(job.seed_dir, rerated=True)  # finalize rewrote TRCK/POPM, keeping mtimes
        self.bus.publish("set_changed", {"folder": job.folder})
        self.crates.queue_analysis(job.seed_dir)

    def shutdown(self) -> None:
        self._preview_q.put(None)
        self.bus.close()
        self.jobs.shutdown(timeout=2.0)
        self.analysis.shutdown(timeout=1.0)
        self.stems.shutdown(timeout=1.0)
        self.setmixes.shutdown()
        self.mixes.shutdown()
        with self._lock:
            mgr, self._previews = self._previews, None
        if mgr is not None:
            mgr.shutdown()


# --- the app ------------------------------------------------------------------------------------------------------


def create_app(
    settings: Any,
    static_dir: Path | None = None,
    *,
    dev: bool = False,
    cache_dir: Path | None = None,
    save_settings: Callable[[Any], None] | None = None,
    tunebat: TunebatService | None = None,
    heartbeat_s: float = HEARTBEAT_S,
    analysis_workers: int | None = None,
) -> FastAPI:
    """The Seedbox API.

    `cache_dir` defaults to musicreco.settings.cache_dir() (previews go in <cache_dir>/previews), `save_settings`
    to musicreco.settings.save_settings; both are injectable for tests, as are the tunebat service and timings.
    """
    state = AppState(settings, cache_dir, save_settings, tunebat, heartbeat_s, analysis_workers)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await asyncio.to_thread(state.shutdown)

    app = FastAPI(title="Seedbox", version=app_version(), lifespan=lifespan, docs_url="/api/docs" if dev else None,
                  redoc_url=None, openapi_url="/api/openapi.json" if dev else None)  # fmt: skip
    app.state.seedbox = state
    if dev:
        app.add_middleware(CORSMiddleware, allow_origins=DEV_ORIGINS, allow_methods=["*"], allow_headers=["*"])
    _add_error_handlers(app)

    # ---- system + settings

    @app.get("/api/health")
    def health() -> Response:
        return JSON({"ok": True})

    @app.get("/api/system")
    def system() -> Response:
        yt = ytdlp_fields()
        return JSON({
            "version": app_version(),
            "ytdlp_version": str(yt.get("ytdlp_version") or "unknown"),
            "ytdlp_latest": yt.get("ytdlp_latest"),
            "ffmpeg": engine.ffmpeg_path(),
            "deno": engine.deno_path(),
            "output_root": str(state.root),
        })  # fmt: skip

    @app.post("/api/system/update-ytdlp")
    def update_ytdlp() -> Response:
        fn = getattr(_updater(), "update_ytdlp", None)
        if not callable(fn):
            raise ApiError("yt-dlp updates aren't available in this build", 501)
        result = fn()  # {ok, version, message}; never raises, refuses a second concurrent update itself
        return JSON({"ok": bool(result.get("ok")), "version": str(result.get("version") or ""),
                     "message": str(result.get("message") or "")})  # fmt: skip

    @app.post("/api/system/pick-folder")
    def pick_folder(body: dict | None = Body(None)) -> Response:  # noqa: B008
        picker = _folder_picker
        if picker is None:
            raise ApiError("choosing a folder needs the Seedbox window (not available in a browser)", 501)
        initial = (body or {}).get("initial") if isinstance(body, dict) else None
        if not isinstance(initial, str) or not Path(initial).expanduser().is_dir():
            initial = str(state.root) if state.root.is_dir() else str(Path.home())
        if not _picker_busy.acquire(blocking=False):
            raise ApiError("a folder dialog is already open", 409)
        try:
            chosen = picker(initial)
        except Exception as e:  # noqa: BLE001
            log.exception("folder picker failed")
            raise ApiError(f"couldn't open the folder dialog: {e}", 500) from e
        finally:
            _picker_busy.release()
        if isinstance(chosen, (list, tuple)):
            chosen = chosen[0] if chosen else None
        return JSON({"path": str(Path(chosen).expanduser()) if chosen else None})

    @app.get("/api/settings")
    def get_settings() -> Response:
        return JSON(engine.settings_to_json(state.settings))

    @app.put("/api/settings")
    def put_settings(patch: dict = Body(...)) -> Response:  # noqa: B008
        return JSON(engine.settings_to_json(state.update_settings(patch)))

    # ---- search + crate

    @app.get("/api/search")
    def search(term: str = Query(..., min_length=1), page: int = Query(1, ge=1, le=1000)) -> Response:
        return JSON(serializers.search_page(state.tunebat.lookup(term, page)))

    @app.get("/api/crates/new/{seed_id}")
    def new_crate(seed_id: str) -> Response:
        _check_id(seed_id)
        settings = state.settings  # read once: a PUT /api/settings applies from the next crate open
        tol = float(settings.bpm_tolerance)
        pretick_all = bool(getattr(settings, "pretick_all", False))
        detail = state.tunebat.get_track(seed_id)
        seed = detail.seed
        recs, scores = engine.rank(seed, engine.unique_recs(seed, detail.recommended), tol)
        folder = engine.crate_folder_name(seed)
        path = state.root / folder
        index = library.LibraryIndex(path) if path.is_dir() else None

        def existing(t: Track) -> Path | None:
            if index is None:
                return None
            here = [p for p in index.find(t.id) if p.parent == path]
            return here[0] if here else None

        crate = {
            "folder": folder,
            "path": str(path),
            "seed": serializers.track(seed),
            "seed_existing_path": str(p) if (p := existing(seed)) else None,
            "recs": [serializers.rec(t, scores[t.id], engine.passes_rule(seed, t, tol)[1], existing(t), pretick_all)
                     for t in recs],
        }
        state.preload([seed, *recs])
        return JSON(crate)

    # ---- previews

    @app.get("/api/previews")
    def preview_statuses(ids: str = Query("", max_length=20_000)) -> Response:
        wanted = list(dict.fromkeys(i for i in (x.strip() for x in ids.split(",")) if i))
        if len(wanted) > 500:
            raise ApiError("at most 500 ids per request")
        for tid in wanted:
            _check_id(tid)
        mgr = state.previews()
        return JSON([mgr.status(tid) for tid in wanted])

    @app.get("/api/previews/{track_id}")
    def preview_status(track_id: str) -> Response:
        _check_id(track_id)
        return JSON(state.previews().status(track_id))

    @app.post("/api/previews/{track_id}/prioritize")
    def preview_prioritize(track_id: str) -> Response:
        _check_id(track_id)
        mgr = state.previews()
        if engine.accepts(mgr.prioritize, "track"):
            return JSON(mgr.prioritize(track_id, state.tunebat.track(track_id)))
        return JSON(mgr.prioritize(track_id))

    @app.get("/api/previews/{track_id}/audio")
    def preview_audio(track_id: str) -> Response:
        _check_id(track_id)
        path = state.previews().audio_path(track_id)
        if path is None or not Path(path).is_file():
            raise NotFound("preview not ready")
        if not inside(Path(path), state.cache_dir):
            raise ApiError("preview outside the cache folder", 403)
        return FileResponse(path, media_type="audio/mp4")

    # ---- jobs

    @app.post("/api/jobs")
    def start_job(req: JobRequest) -> Response:
        _check_id(req.seed_id)
        if not req.track_ids:
            raise ApiError("no tracks selected")
        tol = float(state.settings.bpm_tolerance)
        detail = state.tunebat.get_track(req.seed_id)
        seed = detail.seed
        recs, scores = engine.rank(seed, engine.unique_recs(seed, detail.recommended), tol)
        everything = [seed, *recs]
        known = {t.id for t in everything}
        if req.seed_id not in known:  # tunebat answered with a relinked ID for the seed
            known.add(req.seed_id)
        unknown = [i for i in req.track_ids if i not in known]
        if unknown:
            raise ApiError(f"not part of this crate: {', '.join(unknown[:5])}")
        wanted = set(req.track_ids)
        if req.seed_id in wanted:
            wanted.add(seed.id)
        selected = [t for t in everything if t.id in wanted]
        try:
            job = state.jobs.start(seed, recs, scores, selected, library.seed_dir(seed, root=state.root))
        except RuntimeError as e:
            raise ApiError(str(e), 503) from e
        return JSON(job.to_json())

    @app.get("/api/jobs")
    def list_jobs() -> Response:
        return JSON([j.to_json() for j in state.jobs.list()])

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> Response:
        job = state.jobs.get(job_id)
        if job is None:
            raise NotFound("no such job")
        return JSON(job.to_json())

    @app.delete("/api/jobs/{job_id}")
    def cancel_job(job_id: str) -> Response:
        job = state.jobs.cancel(job_id)
        if job is None:
            raise NotFound("no such job")
        return JSON(job.to_json())

    # ---- crate library + set builder

    @app.get("/api/crates")
    def crates() -> Response:
        out = []
        for c in library.list_crates(state.root):
            out.append({
                "folder": c["folder"],
                "path": str(c["path"]),
                "seed": state.crates.summary_seed(Path(c["path"]), track_json(c.get("seed"))),
                "track_count": int(c.get("track_count") or 0),
                "updated_at": str(c.get("updated_at") or ""),
            })  # fmt: skip
        return JSON(out)

    def with_styles(data: dict) -> dict:
        """CrateSet + the planned style of each transition (the set's style chain); the set works without them."""
        try:
            styles = state.mixes.crate_styles(data["folder"])
        except Exception:  # noqa: BLE001
            log.exception("planning the styles of %s failed", data.get("folder"))
            return data
        for t, st in zip(data["transitions"], styles):
            t["style"] = st
        return data

    @app.get("/api/crates/{folder}/set")
    def crate_set(folder: str) -> Response:
        return JSON(with_styles(state.crates.crate_set(folder)))

    @app.put("/api/crates/{folder}/order")
    def crate_order(folder: str, req: OrderRequest) -> Response:
        return JSON(with_styles(state.crates.set_order(folder, req.track_ids)))

    @app.post("/api/crates/{folder}/auto-order")
    def crate_auto_order(folder: str) -> Response:
        return JSON(with_styles(state.crates.auto_order(folder)))

    @app.get("/api/crates/{folder}/stems")
    def crate_stems(folder: str) -> Response:
        return JSON(state.crates.stems_status(folder))

    @app.get("/api/crates/{folder}/partners/{track_id}")
    def crate_partners(folder: str, track_id: str, direction: str = Query("out")) -> Response:
        return JSON(state.crates.partners(folder, track_id, direction))

    # ---- v4 mix plans + rendered transitions

    @app.get("/api/crates/{folder}/mix/{from_id}/{to_id}")
    def mix_plan(folder: str, from_id: str, to_id: str, technique: str | None = Query(None),
                 bars: int | None = Query(None), style: str | None = Query(None),
                 swap: str | None = Query(None)) -> Response:  # fmt: skip
        return JSON(state.mixes.plan(folder, from_id, to_id, technique, bars, style, swap))

    @app.put("/api/crates/{folder}/mix/{from_id}/{to_id}")
    def mix_save(folder: str, from_id: str, to_id: str, body: Any = Body(...)) -> Response:  # noqa: B008
        return JSON(state.mixes.save(folder, from_id, to_id, body))

    @app.delete("/api/crates/{folder}/mix/{from_id}/{to_id}")
    def mix_reset(folder: str, from_id: str, to_id: str) -> Response:
        return JSON(state.mixes.delete(folder, from_id, to_id))

    @app.post("/api/crates/{folder}/mix/{from_id}/{to_id}/render")
    def mix_render(folder: str, from_id: str, to_id: str, body: Any = Body(None)) -> Response:  # noqa: B008
        return JSON(state.mixes.render(folder, from_id, to_id, body))

    @app.get("/api/renders/{render_id}.m4a")
    def render_audio(render_id: str) -> Response:
        return FileResponse(state.mixes.audio_path(render_id), media_type="audio/mp4")

    # ---- full set mixes (one continuous MP3 of a crate's set or a setlist)

    @app.post("/api/crates/{folder}/setmix")
    def crate_setmix_start(folder: str) -> Response:
        return JSON(state.setmixes.start(f"crate:{folder}"))

    @app.get("/api/crates/{folder}/setmix")
    def crate_setmix(folder: str) -> Response:
        return JSON(state.setmixes.current(f"crate:{folder}"))

    @app.get("/api/setmix/{mix_id}")
    def setmix_get(mix_id: str) -> Response:
        return JSON(state.setmixes.get(mix_id))

    @app.delete("/api/setmix/{mix_id}")
    def setmix_cancel(mix_id: str) -> Response:
        return JSON(state.setmixes.cancel(mix_id))

    @app.get("/api/setmix/{mix_id}/audio")
    def setmix_audio(mix_id: str) -> Response:
        path = state.setmixes.audio_path(mix_id)
        return FileResponse(path, media_type="audio/mpeg" if path.suffix.lower() == ".mp3" else "audio/mp4")

    # ---- setlists (crates back to back) + crate-to-crate links

    @app.get("/api/crates/{folder}/links")
    def crate_links(folder: str) -> Response:
        return JSON(state.setlists.links(folder))

    @app.get("/api/setlists")
    def setlists() -> Response:
        return JSON(state.setlists.list())

    @app.post("/api/setlists")
    def setlist_create(body: dict = Body(...)) -> Response:  # noqa: B008
        return JSON(state.setlists.create(body.get("name"), body.get("crates")))

    @app.get("/api/setlists/{sid}")
    def setlist_get(sid: str) -> Response:
        return JSON(state.setlists.get(sid))

    @app.put("/api/setlists/{sid}")
    def setlist_update(sid: str, body: dict = Body(...)) -> Response:  # noqa: B008
        return JSON(state.setlists.update(sid, body))

    @app.delete("/api/setlists/{sid}")
    def setlist_delete(sid: str) -> Response:
        return JSON(state.setlists.delete(sid))

    @app.post("/api/setlists/{sid}/auto-order")
    def setlist_auto_order(sid: str) -> Response:
        return JSON(state.setlists.auto_order(sid))

    @app.get("/api/setlists/{sid}/mix/{from_id}/{to_id}")
    def setlist_mix_plan(sid: str, from_id: str, to_id: str, technique: str | None = Query(None),
                         bars: int | None = Query(None), style: str | None = Query(None),
                         swap: str | None = Query(None)) -> Response:  # fmt: skip
        return JSON(state.mixes.plan_pair(state.setlists.pair(sid, from_id, to_id), technique, bars, style=style,
                                          swap=swap))

    @app.put("/api/setlists/{sid}/mix/{from_id}/{to_id}")
    def setlist_mix_save(sid: str, from_id: str, to_id: str, body: Any = Body(...)) -> Response:  # noqa: B008
        return JSON(state.mixes.save_pair(state.setlists.pair(sid, from_id, to_id), body))

    @app.delete("/api/setlists/{sid}/mix/{from_id}/{to_id}")
    def setlist_mix_reset(sid: str, from_id: str, to_id: str) -> Response:
        return JSON(state.mixes.delete_pair(state.setlists.pair(sid, from_id, to_id)))

    @app.post("/api/setlists/{sid}/mix/{from_id}/{to_id}/render")
    def setlist_mix_render(sid: str, from_id: str, to_id: str, body: Any = Body(None)) -> Response:  # noqa: B008
        return JSON(state.mixes.render_pair(state.setlists.pair(sid, from_id, to_id), body))

    @app.post("/api/setlists/{sid}/setmix")
    def setlist_setmix_start(sid: str) -> Response:
        return JSON(state.setmixes.start(f"setlist:{sid}"))

    @app.get("/api/setlists/{sid}/setmix")
    def setlist_setmix(sid: str) -> Response:
        return JSON(state.setmixes.current(f"setlist:{sid}"))

    @app.get("/api/analysis/{folder}/{track_id}")
    def analysis(folder: str, track_id: str) -> Response:
        return JSON(state.crates.track_analysis(folder, track_id))

    @app.get("/api/covers/{folder}/{track_id}")
    def cover(folder: str, track_id: str, request: Request) -> Response:
        _check_id(track_id)
        _path, mime, data = state.crates.cover(folder, track_id)
        etag = '"' + hashlib.md5(data, usedforsecurity=False).hexdigest()[:16] + '"'
        headers = {"Cache-Control": "public, max-age=86400", "ETag": etag}
        if etag in request.headers.get("if-none-match", ""):
            return Response(status_code=304, headers=headers)
        return Response(data, media_type=mime, headers=headers)

    @app.get("/api/files/{folder}/{filename}")
    def crate_file(folder: str, filename: str) -> Response:
        path = resolve_crate_file(state.root, folder, filename)
        media = "audio/mpeg" if path.suffix.lower() == ".mp3" else None
        return FileResponse(path, media_type=media)

    @app.post("/api/reveal")
    def reveal(req: RevealRequest) -> Response:
        path = Path(req.path).expanduser()
        if not path.is_absolute():
            raise ApiError("path must be absolute")
        if not inside(path, state.root):
            raise ApiError("only files in the crates folder can be revealed", 403)
        if not path.exists():
            raise NotFound("no such file or folder")
        if sys.platform != "darwin":
            raise ApiError("reveal in Finder is macOS-only", 501)
        open_in_finder(path)
        return JSON({"ok": True})

    # ---- events

    @app.get("/api/events")
    async def events(request: Request) -> Response:
        async def disconnected() -> None:
            while (await request.receive())["type"] != "http.disconnect":
                pass

        return StreamingResponse(
            state.bus.stream(disconnected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- frontend

    if static_dir is not None:
        _add_spa(app, Path(static_dir))
    return app


def _check_id(track_id: str) -> None:
    if not _TRACK_ID.match(track_id):
        raise ApiError("invalid track id")


def _add_spa(app: FastAPI, static_dir: Path) -> None:
    root = static_dir.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> Response:
        if full_path == "api" or full_path.startswith("api/"):
            raise NotFound("Not Found")
        if full_path:
            candidate = root / full_path
            if inside(candidate, root) and candidate.is_file():
                hashed = full_path.startswith("assets/")  # Vite's content-hashed bundles
                return FileResponse(candidate, headers={"Cache-Control": "public, max-age=31536000, immutable"}
                                    if hashed else None)  # fmt: skip
        index = root / "index.html"
        if not index.is_file():
            raise NotFound("frontend not built")
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


def _add_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_req: Request, e: ApiError) -> Response:
        return JSON({"detail": e.detail}, status_code=e.status)

    @app.exception_handler(TunebatError)
    async def tunebat_error(_req: Request, e: TunebatError) -> Response:
        return JSON({"detail": f"tunebat: {e}"}, status_code=502)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_req: Request, e: RequestValidationError) -> Response:
        parts = []
        for err in e.errors():
            where = ".".join(str(x) for x in err.get("loc", ()) if x not in ("body", "query", "path"))
            parts.append(f"{where}: {err.get('msg')}" if where else str(err.get("msg")))
        return JSON({"detail": "; ".join(parts) or "invalid request"}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_req: Request, e: StarletteHTTPException) -> Response:
        return JSON({"detail": e.detail}, status_code=e.status_code, headers=getattr(e, "headers", None))

    @app.exception_handler(Exception)
    async def unexpected(_req: Request, e: Exception) -> Response:
        log.exception("unhandled error")
        return JSON({"detail": f"internal error: {type(e).__name__}: {e}"}, status_code=500)


# --- running it ---------------------------------------------------------------------------------------------------


def _uvicorn_server(app: FastAPI, log_level: str, host: str = "127.0.0.1", port: int = 8765) -> Any:
    import uvicorn

    class _Server(uvicorn.Server):
        async def shutdown(self, sockets: Any = None) -> None:
            app.state.seedbox.bus.close()  # end SSE streams first, or graceful shutdown waits on them
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

    def stop(self, timeout: float = 5.0) -> None:
        self._server.should_exit = True
        self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread.is_alive()


def start_server(
    settings: Any = None,
    static_dir: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    dev: bool = False,
    log_level: str = "warning",
    timeout: float = 15.0,
    folder_picker: FolderPicker | None = None,
    **app_kwargs: Any,
) -> ServerHandle:
    """Start the API (and SPA) on a daemon thread and return once it accepts connections.

    `port=0` picks a free port (read it from handle.port / handle.url). Settings default to load_settings().
    `folder_picker` is the same hook as set_folder_picker (which can also be called later, once a window exists).
    """
    if folder_picker is not None:
        set_folder_picker(folder_picker)
    settings = settings if settings is not None else engine.load_settings()
    app = create_app(settings, static_dir, dev=dev, **app_kwargs)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.set_inheritable(True)
    real_port = sock.getsockname()[1]
    server = _uvicorn_server(app, log_level, host, real_port)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="seedbox-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("the Seedbox server failed to start")
        if time.monotonic() > deadline:
            server.should_exit = True
            raise RuntimeError("the Seedbox server didn't start in time")
        time.sleep(0.02)
    return ServerHandle(f"http://{host}:{real_port}", real_port, app, server, thread)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m seedbox.server", description="Seedbox API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--dev", action="store_true", help="allow the Vite dev server (localhost:5173) via CORS")
    parser.add_argument("--static", type=Path, default=None, help="serve this built frontend (dist/) at /")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.static is not None and not (args.static / "index.html").is_file():
        parser.error(f"--static {args.static}: no index.html there")
    app = create_app(engine.load_settings(), args.static, dev=args.dev)
    _uvicorn_server(app, args.log_level, args.host, args.port).run()


if __name__ == "__main__":
    main()
