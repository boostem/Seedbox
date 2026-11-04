"""/sets: sets in running order (#38); set planning and ordering come with #60–#64.

- `GET /sets/{id}/stems` (#44): the "stems ready for this set" indicator, overall and per track.
- `POST /sets/{id}/stems`: the set is open: its missing stems go to the front of the heavy queue. Same answer.

Events: `stems.set` (`ready`, `ready_tracks`, `total`, `progress`) when a track of the set finishes, and `job.progress`
(with the track's `hash`) while one runs.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from seedbox2.analysis import stems
from seedbox2.library import Library
from seedbox2.server.deps import JobsDep, LibraryDep

router = APIRouter(prefix="/sets", tags=["sets"])


@router.get("")
def sets(lib: LibraryDep) -> list[dict]:
    return lib.sets()


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


def _add(lib: Library, set_id: int, hashes: list[str]) -> int:
    try:
        return lib.add_to_set(set_id, hashes)
    except KeyError:
        raise HTTPException(404, "no such set") from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
