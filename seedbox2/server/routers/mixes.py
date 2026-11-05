"""/mixes: mix plans (#38 stores them). The live engine in the browser plays and auditions them (#65, #67), so nothing
here renders audio to listen to: the server renders for export only (#70)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from seedbox2.mix import convert
from seedbox2.server.deps import LibraryDep

router = APIRouter(prefix="/mixes", tags=["mixes"])


@router.get("/plans")
def plans(lib: LibraryDep, edited: bool | None = None) -> list[dict]:
    def name(hash_: str) -> str:
        t = lib.track(hash_) or {}
        return " - ".join(x for x in (t.get("artist"), t.get("title")) if x) or hash_[:8]

    return [{**{k: v for k, v in p.items() if k != "plan"}, "from_name": name(p["from_hash"]), "to_name": name(p["to_hash"])}
            for p in lib.plans(user_edited=edited)]  # fmt: skip


@router.get("/plans/{from_hash}/{to_hash}")
def plan(from_hash: str, to_hash: str, lib: LibraryDep) -> dict:
    p = lib.plan(from_hash, to_hash)
    if p is None:
        raise HTTPException(404, "no saved plan for this pair")
    return p


@router.get("/plans/{from_hash}/{to_hash}/live")
def live_plan(from_hash: str, to_hash: str, lib: LibraryDep) -> dict:
    """The pair's plan as v3 with the decks on these hashes, for the live engine, whichever version wrote it."""
    p = lib.plan(from_hash, to_hash)
    if p is None:
        raise HTTPException(404, "no saved plan for this pair")
    try:
        plan = convert.load(p["plan"], from_track=from_hash, to_track=to_hash)
    except ValueError as e:
        raise HTTPException(422, f"this plan can't be read: {e}") from e
    return {"plan": plan.model_dump(), "user_edited": p["user_edited"]}
