"""Setbox's API on the shared server (seedbox.server.create_app(app="setbox") calls register): the set builder,
mix plans and rendered transitions, vocal stems, full set mixes and setlists. Seedbox never imports this module, so
it never loads the analysis, mix or stem code.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Body, FastAPI, Query
from pydantic import BaseModel
from starlette.responses import FileResponse, Response

from seedbox.serializers import JSON

from .analysis_queue import AnalysisQueue
from .crates import CrateService
from .mixes import MixService
from .setlists import SetlistService
from .setmixes import SetMixService
from .stems_queue import StemQueue
from .watcher import CrateWatcher

log = logging.getLogger(__name__)


class OrderRequest(BaseModel):
    track_ids: list[str]


class SetboxServices:
    """Setbox's services: analysis workers, the set builder, mix plans / renders, setlists, set mixes, the vocal
    stem queue (songs in use first; the rest of a crate only while nothing renders) and the crate watcher (Seedbox's
    downloads land while Setbox is open)."""

    def __init__(self, state: Any, analysis_workers: int | None = None) -> None:
        self.analysis = AnalysisQueue(state.bus, workers=analysis_workers)
        self.crates = CrateService(lambda: state.settings, self.analysis, state.bus)
        self.mixes = MixService(self.crates, state.bus, lambda: state.cache_dir)
        self.setlists = SetlistService(self.crates, self.mixes, state.bus)
        self.setmixes = SetMixService(self.crates, self.mixes, self.setlists, state.bus)
        self.stems = StemQueue(state.bus, idle=lambda: not (self.mixes.busy() or self.setmixes.busy()))
        self.crates.stems = self.stems
        self.watcher = CrateWatcher(lambda: self.crates.root, state.bus)

    def forget(self) -> None:
        """The crates folder changed: drop what was remembered about the old one."""
        self.analysis.forget()
        self.stems.forget()
        self.watcher.forget()

    def shutdown(self) -> None:
        self.watcher.shutdown()
        self.analysis.shutdown(timeout=1.0)
        self.stems.shutdown(timeout=1.0)
        self.setmixes.shutdown()
        self.mixes.shutdown()


def register(app: FastAPI, state: Any, analysis_workers: int | None = None) -> SetboxServices:
    """Create Setbox's services on `state` (also as state.crates / .mixes / ... for callers and tests) and add its
    endpoints to `app`."""
    sv = SetboxServices(state, analysis_workers)
    state.analysis, state.crates, state.mixes = sv.analysis, sv.crates, sv.mixes
    state.setlists, state.setmixes, state.stems = sv.setlists, sv.setmixes, sv.stems
    state.on_root_change.append(sv.forget)
    state.on_shutdown.append(sv.shutdown)

    def with_styles(data: dict) -> dict:
        """CrateSet + the planned style of each transition (the set's style chain) and, in chill mode, its plan's notes
        (what the mix will do: the suggestion's own notes describe a hand-mixing recipe); the set works without them."""
        try:
            summaries = sv.mixes.crate_summaries(data["folder"])
        except Exception:  # noqa: BLE001
            log.exception("planning the styles of %s failed", data.get("folder"))
            return data
        for t, (st, notes) in zip(data["transitions"], summaries):
            t["style"] = st
            t["plan_notes"] = notes
        return data

    # ---- the set builder

    @app.get("/api/crates/{folder}/set")
    def crate_set(folder: str) -> Response:
        return JSON(with_styles(sv.crates.crate_set(folder)))

    @app.put("/api/crates/{folder}/order")
    def crate_order(folder: str, req: OrderRequest) -> Response:
        return JSON(with_styles(sv.crates.set_order(folder, req.track_ids)))

    @app.post("/api/crates/{folder}/auto-order")
    def crate_auto_order(folder: str) -> Response:
        return JSON(with_styles(sv.crates.auto_order(folder)))

    @app.get("/api/crates/{folder}/stems")
    def crate_stems(folder: str) -> Response:
        return JSON(sv.crates.stems_status(folder))

    @app.get("/api/crates/{folder}/partners/{track_id}")
    def crate_partners(folder: str, track_id: str, direction: str = Query("out")) -> Response:
        return JSON(sv.crates.partners(folder, track_id, direction))

    @app.get("/api/analysis/{folder}/{track_id}")
    def analysis(folder: str, track_id: str) -> Response:
        return JSON(sv.crates.track_analysis(folder, track_id))

    # ---- mix plans + rendered transitions

    @app.get("/api/crates/{folder}/mix/{from_id}/{to_id}")
    def mix_plan(folder: str, from_id: str, to_id: str, technique: str | None = Query(None),
                 bars: int | None = Query(None), style: str | None = Query(None),
                 swap: str | None = Query(None)) -> Response:  # fmt: skip
        return JSON(sv.mixes.plan(folder, from_id, to_id, technique, bars, style, swap))

    @app.put("/api/crates/{folder}/mix/{from_id}/{to_id}")
    def mix_save(folder: str, from_id: str, to_id: str, body: Any = Body(...)) -> Response:  # noqa: B008
        return JSON(sv.mixes.save(folder, from_id, to_id, body))

    @app.delete("/api/crates/{folder}/mix/{from_id}/{to_id}")
    def mix_reset(folder: str, from_id: str, to_id: str) -> Response:
        return JSON(sv.mixes.delete(folder, from_id, to_id))

    @app.post("/api/crates/{folder}/mix/{from_id}/{to_id}/render")
    def mix_render(folder: str, from_id: str, to_id: str, body: Any = Body(None)) -> Response:  # noqa: B008
        return JSON(sv.mixes.render(folder, from_id, to_id, body))

    @app.get("/api/renders/{render_id}.m4a")
    def render_audio(render_id: str) -> Response:
        return FileResponse(sv.mixes.audio_path(render_id), media_type="audio/mp4")

    # ---- full set mixes (one continuous MP3 of a crate's set or a setlist)

    @app.post("/api/crates/{folder}/setmix")
    def crate_setmix_start(folder: str) -> Response:
        return JSON(sv.setmixes.start(f"crate:{folder}"))

    @app.get("/api/crates/{folder}/setmix")
    def crate_setmix(folder: str) -> Response:
        return JSON(sv.setmixes.current(f"crate:{folder}"))

    @app.get("/api/setmix/{mix_id}")
    def setmix_get(mix_id: str) -> Response:
        return JSON(sv.setmixes.get(mix_id))

    @app.delete("/api/setmix/{mix_id}")
    def setmix_cancel(mix_id: str) -> Response:
        return JSON(sv.setmixes.cancel(mix_id))

    @app.get("/api/setmix/{mix_id}/audio")
    def setmix_audio(mix_id: str) -> Response:
        path = sv.setmixes.audio_path(mix_id)
        return FileResponse(path, media_type="audio/mpeg" if path.suffix.lower() == ".mp3" else "audio/mp4")

    # ---- setlists (crates back to back) + crate-to-crate links

    @app.get("/api/crates/{folder}/links")
    def crate_links(folder: str) -> Response:
        return JSON(sv.setlists.links(folder))

    @app.get("/api/setlists")
    def setlists() -> Response:
        return JSON(sv.setlists.list())

    @app.post("/api/setlists")
    def setlist_create(body: dict = Body(...)) -> Response:  # noqa: B008
        return JSON(sv.setlists.create(body.get("name"), body.get("crates")))

    @app.get("/api/setlists/{sid}")
    def setlist_get(sid: str) -> Response:
        return JSON(sv.setlists.get(sid))

    @app.put("/api/setlists/{sid}")
    def setlist_update(sid: str, body: dict = Body(...)) -> Response:  # noqa: B008
        return JSON(sv.setlists.update(sid, body))

    @app.delete("/api/setlists/{sid}")
    def setlist_delete(sid: str) -> Response:
        return JSON(sv.setlists.delete(sid))

    @app.post("/api/setlists/{sid}/auto-order")
    def setlist_auto_order(sid: str) -> Response:
        return JSON(sv.setlists.auto_order(sid))

    @app.get("/api/setlists/{sid}/mix/{from_id}/{to_id}")
    def setlist_mix_plan(sid: str, from_id: str, to_id: str, technique: str | None = Query(None),
                         bars: int | None = Query(None), style: str | None = Query(None),
                         swap: str | None = Query(None)) -> Response:  # fmt: skip
        return JSON(sv.mixes.plan_pair(sv.setlists.pair(sid, from_id, to_id), technique, bars, style=style, swap=swap))

    @app.put("/api/setlists/{sid}/mix/{from_id}/{to_id}")
    def setlist_mix_save(sid: str, from_id: str, to_id: str, body: Any = Body(...)) -> Response:  # noqa: B008
        return JSON(sv.mixes.save_pair(sv.setlists.pair(sid, from_id, to_id), body))

    @app.delete("/api/setlists/{sid}/mix/{from_id}/{to_id}")
    def setlist_mix_reset(sid: str, from_id: str, to_id: str) -> Response:
        return JSON(sv.mixes.delete_pair(sv.setlists.pair(sid, from_id, to_id)))

    @app.post("/api/setlists/{sid}/mix/{from_id}/{to_id}/render")
    def setlist_mix_render(sid: str, from_id: str, to_id: str, body: Any = Body(None)) -> Response:  # noqa: B008
        return JSON(sv.mixes.render_pair(sv.setlists.pair(sid, from_id, to_id), body))

    @app.post("/api/setlists/{sid}/setmix")
    def setlist_setmix_start(sid: str) -> Response:
        return JSON(sv.setmixes.start(f"setlist:{sid}"))

    @app.get("/api/setlists/{sid}/setmix")
    def setlist_setmix(sid: str) -> Response:
        return JSON(sv.setmixes.current(f"setlist:{sid}"))

    return sv
