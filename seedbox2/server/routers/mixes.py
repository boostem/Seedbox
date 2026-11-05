"""/mixes: mix plans (#38 stores them). The live engine in the browser plays and auditions them (#65, #67), so nothing
here renders audio to listen to: the server renders for export only (#70)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from seedbox2.library.service import SETTINGS_FILE, Settings
from seedbox2.mix import convert
from seedbox2.mix import plan as v3
from seedbox2.mix import master as master_chain
from seedbox2.server.deps import LibraryDep

router = APIRouter(prefix="/mixes", tags=["mixes"])


class MasterSettings(BaseModel):
    target: Literal["streaming", "club"]


def _settings(lib: LibraryDep) -> Settings:
    return Settings(Path(lib.db.path).parent / SETTINGS_FILE)


@router.get("/master")
def master(lib: LibraryDep) -> dict:
    """The master chain's loudness target (#58), chosen in Settings: its LUFS, ceiling and open-loop make-up."""
    return master_chain.status(_settings(lib))


@router.post("/master")
def set_master(body: MasterSettings, lib: LibraryDep) -> dict:
    return master_chain.set_target(_settings(lib), body.target)


@router.get("/plans")
def plans(lib: LibraryDep, edited: bool | None = None) -> list[dict]:
    def name(hash_: str) -> str:
        t = lib.track(hash_) or {}
        return " - ".join(x for x in (t.get("artist"), t.get("title")) if x) or hash_[:8]

    return [{**{k: v for k, v in p.items() if k not in ("plan", "auto_plan")}, "from_name": name(p["from_hash"]), "to_name": name(p["to_hash"])}
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
    auto = None
    if p["auto_plan"] is not None:
        try:
            auto = convert.load(p["auto_plan"], from_track=from_hash, to_track=to_hash).model_dump()
        except ValueError:  # the edit still plays; there's just nothing to go back to
            auto = None
    return {"plan": plan.model_dump(), "user_edited": p["user_edited"], "auto": auto}


@router.put("/plans/{from_hash}/{to_hash}")
def save_plan(from_hash: str, to_hash: str, raw: dict, lib: LibraryDep) -> dict:
    """The transition editor's edit (#79). A plan that isn't valid is refused and the saved one stays."""
    try:
        plan = v3.validate(raw)
    except ValueError as e:
        raise HTTPException(422, f"this plan can't be saved: {e}") from e
    if plan.a.track_id != from_hash or plan.b.track_id != to_hash:
        raise HTTPException(422, "this plan is for another pair of tracks")
    if not lib.save_plan_edit(from_hash, to_hash, plan.model_dump()):
        raise HTTPException(404, "no saved plan for this pair")
    return {"saved": True}


@router.post("/plans/{from_hash}/{to_hash}/reset")
def reset_plan(from_hash: str, to_hash: str, lib: LibraryDep) -> dict:
    """Drops the editor's edit: back to the plan it started from. Nothing to go back to is a no-op."""
    if lib.plan(from_hash, to_hash) is None:
        raise HTTPException(404, "no saved plan for this pair")
    return {"reset": lib.reset_plan_edit(from_hash, to_hash)}
