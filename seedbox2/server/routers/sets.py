"""/sets: sets in running order (#38); set planning and ordering come with #60–#64."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from seedbox2.library import Library

from seedbox2.server.deps import LibraryDep

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


def _add(lib: Library, set_id: int, hashes: list[str]) -> int:
    try:
        return lib.add_to_set(set_id, hashes)
    except KeyError:
        raise HTTPException(404, "no such set") from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
