"""/mixes: mix plans (#38 stores them); transitions and renders come with #51–#59, #70."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from seedbox2.server.deps import LibraryDep

router = APIRouter(prefix="/mixes", tags=["mixes"])


@router.get("/plans")
def plans(lib: LibraryDep, edited: bool | None = None) -> list[dict]:
    return [{k: v for k, v in p.items() if k != "plan"} for p in lib.plans(user_edited=edited)]


@router.get("/plans/{from_hash}/{to_hash}")
def plan(from_hash: str, to_hash: str, lib: LibraryDep) -> dict:
    p = lib.plan(from_hash, to_hash)
    if p is None:
        raise HTTPException(404, "no saved plan for this pair")
    return p
