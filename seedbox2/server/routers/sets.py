"""/sets: sets in running order (#38); set planning and ordering come with #60–#64."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

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
