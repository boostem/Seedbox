"""/sets: sets in running order (#38), and ordering them (#60; set planning comes with #61–#64).

- `GET /sets/{id}/stems` (#44): the "stems ready for this set" indicator, overall and per track.
- `POST /sets/{id}/stems`: the set is open: its missing stems go to the front of the heavy queue. Same answer.
- `GET /sets/{id}/session` (#77): what the now-playing view needs to play the set: its tracks with their grid, sections,
  key and loudness, and each neighbouring pair's plan as version 3 (`null` when there is none or it can't be read).

- `GET /sets/arcs`: the energy arc templates.
- `GET /sets/{id}/order`: the set as it stands, explained, with the settings (`arc`, `tempo_start`, `tempo_end`, `pins`)
  it was last ordered with.
- `POST /sets/{id}/order`: the best order found for those settings. Nothing is saved.
- `PUT /sets/{id}/order` with `hashes` too: saves that order and the settings. 409 if the set has other tracks by then.
- `POST /sets/{id}/timeline` with `hashes`, the running order to draw (#75): tracks' sections and energy, and each
  transition's place in its two files. Pairs without a plan come back `pending` and their `set_plan` job is queued
  with the set plan's pick for the pair (#61) (`waiting` while a track's beats are still to come; `retry` tries
  `failed` pairs again). 409 if the tracks aren't the set's.
- `POST /sets/{id}/setplan` (#61) with `hashes` (default: the saved order), `target_minutes`, `long_every`, `lookahead`,
  `reorder` and the order settings: every transition's style, length and cue points and how much of each track plays,
  planned together; with `reorder` the order too. Saves nothing. 409 if the tracks aren't the set's.

- `GET /sets/{id}/bridge?position&limit` (#64): what would fit between that position and the next, from the library.
- `GET /sets/{id}/bridge/online?position&limit`: the same from tunebat's recommendations of both neighbours. 502 if it
  doesn't answer. `POST /tunebat/download` fetches one.
- `POST /sets/{id}/bridge` `{position, hash, from, to}`: puts a track there. 409 if those aren't neighbours there any more.

- `POST /sets/{id}/export` `{format, target?}` (#70): the set as one continuous mix, as a `set_export` job (`wav`, `flac`,
  `mp3`, `aac`). 409 with `pairs` while a transition has no plan. `GET /sets/{id}/export`: the formats, where mixes go and
  the set's latest export job. Progress and cancel are the job's: `job.progress`, `POST /jobs/{id}/cancel`.

Long sets (#62), nothing saved until the result goes to `POST /sets`:
- `POST /sets/build`: the best order for `sources` (`[{kind: "crate"|"set"|"library", id}]`), `max_tracks`, `keep_order`.
- `POST /sets/build/explain` with `hashes`: those tracks as they stand, explained, after a track was put in.
- `POST /sets/bridges` with `sources`: the best link from the end of each to the start of each other.
- `POST /sets/build/bridge` with `hashes`, `position`, `limit`: #64's library search for a gap in those tracks.

Only ready tracks (#99, `seedbox2.library.ready`): adding tracks to a set, ordering, building, bridges, the timeline and
the set plan, the now-playing session and export answer 409 `{message, tracks: [{hash, title, artist, state, missing}]}`
when a track isn't ready, rather than mix it on guesses. Bodies take `use_anyway` (hashes): failed tracks to take all the
same, for this request (`PUT /library/tracks/{hash}/use-anyway` keeps it with the track). Reading a set, its stems and
its order as it stands still works, and so does playing one track (`/tracks`).

Events: `stems.set` (`ready`, `ready_tracks`, `total`, `progress`) when a track of the set finishes, and `job.progress`
(with the track's `hash`) while one runs.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from collections.abc import Iterable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from seedbox2.analysis import beats, energy, key, sections, stems, vibe
from seedbox2.library import Library, ready
from seedbox2.mix import convert
from seedbox2.mix import master as master_chain
from seedbox2.mix.setexport import FORMATS
from seedbox2.server.deps import JobsDep, LibraryDep, TunebatDep, get_jobs, vibe_index
from seedbox2.sets import TEMPLATES, OrderRequest, bridge, evaluate_set, order_set, setplan
from seedbox2.sets import export as set_export
from seedbox2.sets.arcs import ArcError
from seedbox2.sets.longset import (
    MAX_TRACKS,
    SourceError,
    bridges,
    build_long_set,
    explain_hashes,
    find_bridges,
)
from seedbox2.sets.service import live_pins, request_from_settings
from seedbox2.sets.solver import SolverError
from seedbox2.sets.timeline import timeline

log = logging.getLogger(__name__)
router = APIRouter(prefix="/sets", tags=["sets"])
Anyway = Annotated[list[str], Field(max_length=1000)]  # failed tracks to take all the same (#99)
AnywayQuery = Annotated[list[str], Query(max_length=1000)]  # the same, as `?use_anyway=hash&use_anyway=hash`


def _require(lib: Library, hashes: list[str] | None, use_anyway: Iterable[str] = ()) -> None:
    """409 with the tracks that aren't ready and what each is missing."""
    try:
        ready.require(lib, hashes or [], use_anyway)
    except ready.NotReady as e:
        raise HTTPException(409, e.detail()) from None


@router.get("")
def sets(lib: LibraryDep) -> list[dict]:
    return lib.sets()


@router.get("/arcs")
def arcs() -> list[dict]:
    return [a.to_json() for a in TEMPLATES.values()]


@router.post("/build")
def build(body: BuildBody, lib: LibraryDep) -> dict:
    request = body.request()
    try:
        result = build_long_set(lib, [s.model_dump() for s in body.sources], request, max_tracks=body.max_tracks,
                                keep_order=body.keep_order, use_anyway=body.use_anyway)  # fmt: skip
    except ready.NotReady as e:
        raise HTTPException(409, e.detail()) from None
    except SourceError as e:
        raise HTTPException(404, str(e)) from None
    except (ArcError, SolverError) as e:
        raise HTTPException(422, str(e)) from None
    return {**result, "settings": _settings_out(request)}


@router.post("/build/explain")
def build_explain(body: ExplainBody, lib: LibraryDep) -> dict:
    request = body.request()
    try:
        return {**explain_hashes(lib, body.hashes, request, body.use_anyway), "settings": _settings_out(request)}
    except ready.NotReady as e:
        raise HTTPException(409, e.detail()) from None
    except SourceError as e:
        raise HTTPException(404, str(e)) from None
    except (ArcError, SolverError) as e:
        raise HTTPException(422, str(e)) from None


@router.post("/build/bridge")
def build_bridge(body: BuildBridgeBody, request: Request, lib: LibraryDep) -> dict[str, Any]:
    try:
        return find_bridges(lib, vibe_index(request), body.hashes, body.position, body.request(), body.limit,
                            body.use_anyway)  # fmt: skip
    except ready.NotReady as e:
        raise HTTPException(409, e.detail()) from None
    except SourceError as e:
        raise HTTPException(404, str(e)) from None
    except (ArcError, SolverError, bridge.BridgeError) as e:
        raise HTTPException(422, str(e)) from None


@router.post("/bridges")
def crate_bridges(body: BridgesBody, lib: LibraryDep) -> dict:
    try:
        return bridges(lib, [s.model_dump() for s in body.sources], use_anyway=body.use_anyway)
    except ready.NotReady as e:
        raise HTTPException(409, e.detail()) from None
    except SourceError as e:
        raise HTTPException(404, str(e)) from None


@router.get("/{set_id}")
def one_set(set_id: int, lib: LibraryDep) -> dict:
    s = lib.set(set_id)
    if s is None:
        raise HTTPException(404, "no such set")
    return s


class NewSet(BaseModel):
    name: str
    hashes: list[str] = []
    use_anyway: Anyway = []


class HashesBody(BaseModel):
    hashes: list[str]
    use_anyway: Anyway = []


@router.post("", status_code=201)
def new_set(body: NewSet, lib: LibraryDep) -> dict:
    if not body.name.strip():
        raise HTTPException(422, "a set needs a name")
    _require(lib, body.hashes, body.use_anyway)
    set_id = lib.save_set(body.name.strip(), [])
    _add(lib, set_id, body.hashes)
    return lib.set(set_id)  # type: ignore[return-value]


@router.post("/{set_id}/tracks")
def add_tracks(set_id: int, body: HashesBody, lib: LibraryDep) -> dict:
    """Append tracks to the end of the set (dragged onto it in the library). 409 if one isn't ready."""
    _require(lib, body.hashes, body.use_anyway)
    return {"added": _add(lib, set_id, body.hashes), "hashes": lib.set_hashes(set_id)}


@router.get("/{set_id}/hashes")
def set_hashes(set_id: int, lib: LibraryDep) -> dict:
    hashes = lib.set_hashes(set_id)
    if hashes is None:
        raise HTTPException(404, "no such set")
    return {"hashes": hashes}


@router.get("/{set_id}/stems")
def set_stems(set_id: int, lib: LibraryDep) -> dict:
    status = stems.set_status(lib, set_id)
    if status is None:
        raise HTTPException(404, "no such set")
    return status


@router.post("/{set_id}/stems")
def open_set_stems(set_id: int, lib: LibraryDep, jobs: JobsDep) -> dict:
    if lib.set_hashes(set_id) is None:
        raise HTTPException(404, "no such set")
    stems.submit_for_set(jobs, lib, set_id)
    return stems.set_status(lib, set_id)  # type: ignore[return-value]


class Pin(BaseModel):
    position: int = Field(ge=0)
    hash: str


class OrderBody(BaseModel):
    arc: str | list[list[float]] | None = None  # a template's name, or a drawn curve as [[time, level], ...]
    tempo_start: float | None = None
    tempo_end: float | None = None
    pins: list[Pin] = []
    note: str | None = Field(default=None, max_length=4000)  # the assistant's reasoning, kept with its set (#63)
    use_anyway: Anyway = []

    def request(self) -> OrderRequest:
        return OrderRequest(arc=self.arc, tempo_start=self.tempo_start, tempo_end=self.tempo_end,
                            pins=[p.model_dump() for p in self.pins], note=self.note)  # fmt: skip


class ApplyOrderBody(OrderBody):
    hashes: list[str]


class SourceBody(BaseModel):
    kind: Literal["crate", "set", "library"]
    id: int | None = None


class BuildBody(OrderBody):
    sources: list[SourceBody] = Field(min_length=1, max_length=50)
    max_tracks: int | None = Field(default=None, ge=2, le=MAX_TRACKS)
    keep_order: bool = True


class ExplainBody(OrderBody):
    hashes: list[str] = Field(max_length=MAX_TRACKS + 50)


class BridgesBody(BaseModel):
    sources: list[SourceBody] = Field(min_length=1, max_length=50)
    use_anyway: Anyway = []


class BuildBridgeBody(ExplainBody):
    position: int = Field(ge=0)
    limit: int = Field(default=bridge.DEFAULT_LIMIT, ge=1, le=50)


def _settings_out(request: OrderRequest) -> dict:
    out = {"arc": request.arc, "tempo_start": request.tempo_start, "tempo_end": request.tempo_end, "pins": request.pins}
    return {**out, "note": request.note} if request.note else out


@router.get("/{set_id}/order")
def current_order(set_id: int, lib: LibraryDep) -> dict:
    hashes = lib.set_hashes(set_id)
    if hashes is None:
        raise HTTPException(404, "no such set")
    request = request_from_settings(lib.set_order_settings(set_id))
    request.pins = live_pins(hashes, request.pins)  # the set may have changed since
    return {**evaluate_set(lib, set_id, request), "settings": _settings_out(request)}


@router.post("/{set_id}/order")
def solve_order(set_id: int, body: OrderBody, lib: LibraryDep) -> dict:
    request = body.request()
    _require(lib, lib.set_hashes(set_id), body.use_anyway)
    try:
        result = order_set(lib, set_id, request)
    except (ArcError, SolverError) as e:
        raise HTTPException(422, str(e)) from None
    if result is None:
        raise HTTPException(404, "no such set")
    return {**result, "settings": _settings_out(request)}


@router.put("/{set_id}/order")
def apply_order(set_id: int, body: ApplyOrderBody, lib: LibraryDep) -> dict:
    hashes = lib.set_hashes(set_id)
    if hashes is None:
        raise HTTPException(404, "no such set")
    if Counter(hashes) != Counter(body.hashes):
        raise HTTPException(409, "the set's tracks have changed: order it again")
    _require(lib, hashes, body.use_anyway)
    request = body.request()
    if Counter((p["position"], p["hash"]) for p in request.pins) - Counter(enumerate(body.hashes)):
        raise HTTPException(422, "a pinned track isn't at its position in this order")
    try:
        lib.put_set_order(set_id, body.hashes, request.settings())
        result = evaluate_set(lib, set_id, request)
    except (ArcError, SolverError) as e:
        raise HTTPException(422, str(e)) from None
    return {**result, "settings": _settings_out(request)}  # type: ignore[dict-item]
def _session_track(lib: Library, hash_: str) -> dict | None:
    track = lib.track(hash_)
    if track is None:
        return None
    grid = lib.analysis(hash_, beats.ANALYSER, beats.VERSION)
    curve = lib.analysis(hash_, energy.ANALYSER, energy.VERSION)
    level = lib.analysis(hash_, energy.LOUDNESS_ANALYSER, energy.VERSION)
    secs = lib.analysis(hash_, sections.ANALYSER)  # the newest, as the sections are read (vibe.sections_of)
    return {
        "hash": hash_, "title": track.get("title") or next((Path(p).stem for p in track["paths"]), None), "artist": track.get("artist"), "duration": track.get("duration"),
        "grid": {k: grid["data"].get(k) for k in ("bpm", "first_downbeat_s", "beats_per_bar", "segments")} if grid else None,
        "phrases": [{"start_s": p["start_s"], "level": p["length"]} for p in (secs["data"].get("phrases") or [])] if secs and isinstance(secs["data"], dict) else [],
        "sections": vibe.spans_of(lib, hash_, curve["data"] if curve else None),
        "key": key.effective(lib, hash_),
        "integrated_lufs": (level["data"].get("integrated_lufs") if level else None),
        "track_gain_db": set_export.track_gain_db(lib, hash_),
    }  # fmt: skip


def _session_plan(lib: Library, from_hash: str, to_hash: str) -> dict | None:
    row = lib.plan(from_hash, to_hash)
    if row is None:
        return None
    try:
        return convert.load(row["plan"], from_track=from_hash, to_track=to_hash).model_dump()
    except ValueError:  # pydantic's ValidationError is one: a plan this version can't read doesn't stop the set
        log.warning("the plan %s -> %s can't be read", from_hash, to_hash, exc_info=True)
        return None


@router.get("/{set_id}/session")
def set_session(set_id: int, lib: LibraryDep, use_anyway: AnywayQuery = []) -> dict:  # noqa: B006
    s = lib.set(set_id)
    hashes = lib.set_hashes(set_id)
    if s is None or hashes is None:
        raise HTTPException(404, "no such set")
    _require(lib, hashes, use_anyway)
    tracks = [t for t in (_session_track(lib, h) for h in hashes) if t]
    return {
        "id": set_id, "name": s["name"], "tracks": tracks,
        "plans": [_session_plan(lib, a["hash"], b["hash"]) for a, b in zip(tracks, tracks[1:], strict=False)],
    }  # fmt: skip


def _gap(lib: Library, set_id: int, position: int, use_anyway: list[str]) -> tuple[bridge.Gap, list[str]]:
    try:
        found = bridge.set_gap(lib, set_id, position)
    except (ArcError, SolverError, bridge.BridgeError) as e:
        raise HTTPException(422, str(e)) from None
    if found is None:
        raise HTTPException(404, "no such set")
    _require(lib, [found[0].a.hash, found[0].b.hash], use_anyway)  # the two tracks either side of the gap
    return found


Limit = Query(default=bridge.DEFAULT_LIMIT, ge=1, le=50)


@router.get("/{set_id}/bridge")
def find_bridge(set_id: int, position: int, request: Request, lib: LibraryDep, limit: int = Limit,
                use_anyway: AnywayQuery = []) -> dict[str, Any]:  # fmt: skip
    gap, hashes = _gap(lib, set_id, position, use_anyway)
    return bridge.search_library(lib, vibe_index(request), gap, hashes, limit)


@router.get("/{set_id}/bridge/online")
def find_bridge_online(set_id: int, position: int, lib: LibraryDep, tunebat: TunebatDep, limit: int = Limit,
                       use_anyway: AnywayQuery = []) -> dict[str, Any]:  # fmt: skip
    from seedbox2.library.tunebat_try import TunebatUnavailable

    gap, _ = _gap(lib, set_id, position, use_anyway)
    try:
        return bridge.search_online(lib, tunebat, gap, limit)
    except TunebatUnavailable:
        raise HTTPException(502, "Tunebat isn't answering. Check the connection and try again.") from None


class InsertBridge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    position: int = Field(ge=0)
    hash: str
    from_hash: str = Field(alias="from")
    to_hash: str = Field(alias="to")
    use_anyway: Anyway = []


@router.post("/{set_id}/bridge")
def insert_bridge(set_id: int, body: InsertBridge, lib: LibraryDep) -> dict:
    """The set with the track in it, explained as `GET /sets/{id}/order` does. 409 if the track isn't ready."""
    _require(lib, [body.hash], body.use_anyway)
    try:
        request = bridge.insert_bridge(lib, set_id, body.position, body.hash, body.from_hash, body.to_hash)
    except bridge.BridgeError as e:
        raise HTTPException(409, str(e)) from None
    if request is None:
        raise HTTPException(404, "no such set")
    return {**evaluate_set(lib, set_id, request), "settings": _settings_out(request)}  # type: ignore[dict-item]


class TimelineBody(BaseModel):
    hashes: list[str]
    retry: bool = False
    use_anyway: Anyway = []


@router.post("/{set_id}/timeline")
def set_timeline(set_id: int, body: TimelineBody, request: Request, lib: LibraryDep) -> dict:
    hashes = lib.set_hashes(set_id)
    if hashes is None:
        raise HTTPException(404, "no such set")
    if Counter(hashes) != Counter(body.hashes):
        raise HTTPException(409, "the set's tracks have changed")
    _require(lib, hashes, body.use_anyway)
    order_request = request_from_settings(lib.set_order_settings(set_id))
    return timeline(lib, body.hashes, get_jobs(request), retry=body.retry, use_anyway=body.use_anyway,
                    picks=lambda: setplan.picks(lib, body.hashes, order_request))  # fmt: skip


class SetPlanBody(OrderBody):
    hashes: list[str] | None = None
    target_minutes: float | None = Field(default=None, gt=0, le=24 * 60)
    long_every: int = Field(default=setplan.LONG_EVERY, ge=1, le=16)
    lookahead: int = Field(default=setplan.LOOKAHEAD, ge=3, le=5)
    reorder: bool = False


@router.post("/{set_id}/setplan")
def plan_set(set_id: int, body: SetPlanBody, lib: LibraryDep) -> dict:
    hashes = lib.set_hashes(set_id)
    if hashes is None:
        raise HTTPException(404, "no such set")
    order = body.hashes if body.hashes is not None else hashes
    if Counter(hashes) != Counter(order):
        raise HTTPException(409, "the set's tracks have changed")
    _require(lib, hashes, body.use_anyway)
    try:
        return setplan.plan_hashes(lib, order, body.request(), long_every=body.long_every, reorder=body.reorder,
                                   target_s=body.target_minutes * 60 if body.target_minutes else None,
                                   lookahead=body.lookahead)  # fmt: skip
    except (ArcError, SolverError) as e:
        raise HTTPException(422, str(e)) from None


class ExportBody(BaseModel):
    format: Literal["wav", "flac", "mp3", "aac"]
    target: Literal["streaming", "club"] | None = None
    use_anyway: Anyway = []


def _export_jobs(lib: Library, set_id: int) -> list[dict]:
    return [j for j in lib.jobs() if j.get("kind") == set_export.JOB and isinstance(j.get("payload"), dict) and j["payload"].get("set_id") == set_id]


@router.get("/{set_id}/export")
def export_status(set_id: int, lib: LibraryDep) -> dict:
    if lib.set_hashes(set_id) is None:
        raise HTTPException(404, "no such set")
    jobs = sorted(_export_jobs(lib, set_id), key=lambda j: j.get("created_at") or "")
    return {"formats": [{"id": f.name, "label": f.label} for f in FORMATS.values()], "folder": str(set_export.mix_folder()),
            "targets": master_chain.TARGETS, "job": jobs[-1] if jobs else None}  # fmt: skip


@router.post("/{set_id}/export", status_code=202)
def export_set(set_id: int, body: ExportBody, lib: LibraryDep, jobs: JobsDep) -> dict:
    s = lib.set(set_id)
    if s is None:
        raise HTTPException(404, "no such set")
    hashes = lib.set_hashes(set_id)
    _require(lib, hashes, body.use_anyway)
    try:
        set_export.check(lib, hashes)
    except set_export.NotReady as e:
        raise HTTPException(409, {"message": str(e), "pairs": e.pairs}) from None
    anyway = [h for h in dict.fromkeys(hashes or []) if h in body.use_anyway]
    return jobs.submit(set_export.JOB, {"set_id": set_id, "hashes": hashes, "format": body.format, "target": body.target, "name": s["name"],
                                        **({"use_anyway": anyway} if anyway else {})}, priority="set")  # fmt: skip


def _add(lib: Library, set_id: int, hashes: list[str]) -> int:
    try:
        return lib.add_to_set(set_id, hashes)
    except KeyError:
        raise HTTPException(404, "no such set") from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
