"""/sets: sets in running order (#38), and ordering them (#60; set planning comes with #61–#64).

- `GET /sets/{id}/stems` (#44): the "stems ready for this set" indicator, overall and per track.
- `POST /sets/{id}/stems`: the set is open: its missing stems go to the front of the heavy queue. Same answer.

- `GET /sets/arcs`: the energy arc templates.
- `GET /sets/{id}/order`: the set as it stands, explained, with the settings (`arc`, `tempo_start`, `tempo_end`, `pins`)
  it was last ordered with.
- `POST /sets/{id}/order`: the best order found for those settings. Nothing is saved.
- `PUT /sets/{id}/order` with `hashes` too: saves that order and the settings. 409 if the set has other tracks by then.

Events: `stems.set` (`ready`, `ready_tracks`, `total`, `progress`) when a track of the set finishes, and `job.progress`
(with the track's `hash`) while one runs.
"""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from seedbox2.analysis import stems
from seedbox2.library import Library
from seedbox2.server.deps import JobsDep, LibraryDep
from seedbox2.sets import TEMPLATES, OrderRequest, evaluate_set, order_set
from seedbox2.sets.arcs import ArcError
from seedbox2.sets.service import live_pins, request_from_settings
from seedbox2.sets.solver import SolverError

router = APIRouter(prefix="/sets", tags=["sets"])


@router.get("")
def sets(lib: LibraryDep) -> list[dict]:
    return lib.sets()


@router.get("/arcs")
def arcs() -> list[dict]:
    return [a.to_json() for a in TEMPLATES.values()]


@router.get("/{set_id}")
def one_set(set_id: int, lib: LibraryDep) -> dict:
    s = lib.set(set_id)
    if s is None:
        raise HTTPException(404, "no such set")
    return s


class NewSet(BaseModel):
    name: str
    hashes: list[str] = []


class HashesBody(BaseModel):
    hashes: list[str]


@router.post("", status_code=201)
def new_set(body: NewSet, lib: LibraryDep) -> dict:
    if not body.name.strip():
        raise HTTPException(422, "a set needs a name")
    set_id = lib.save_set(body.name.strip(), [])
    _add(lib, set_id, body.hashes)
    return lib.set(set_id)  # type: ignore[return-value]


@router.post("/{set_id}/tracks")
def add_tracks(set_id: int, body: HashesBody, lib: LibraryDep) -> dict:
    """Append tracks to the end of the set (dragged onto it in the library)."""
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

    def request(self) -> OrderRequest:
        return OrderRequest(arc=self.arc, tempo_start=self.tempo_start, tempo_end=self.tempo_end,
                            pins=[p.model_dump() for p in self.pins])  # fmt: skip


class ApplyOrderBody(OrderBody):
    hashes: list[str]


def _settings_out(request: OrderRequest) -> dict:
    return {"arc": request.arc, "tempo_start": request.tempo_start, "tempo_end": request.tempo_end, "pins": request.pins}


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
    request = body.request()
    if Counter((p["position"], p["hash"]) for p in request.pins) - Counter(enumerate(body.hashes)):
        raise HTTPException(422, "a pinned track isn't at its position in this order")
    try:
        lib.put_set_order(set_id, body.hashes, request.settings())
        result = evaluate_set(lib, set_id, request)
    except (ArcError, SolverError) as e:
        raise HTTPException(422, str(e)) from None
    return {**result, "settings": _settings_out(request)}  # type: ignore[dict-item]


def _add(lib: Library, set_id: int, hashes: list[str]) -> int:
    try:
        return lib.add_to_set(set_id, hashes)
    except KeyError:
        raise HTTPException(404, "no such set") from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
