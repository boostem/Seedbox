"""/onboarding: whether to show the first-run flow (#80), and /tunebat: its "try one track" path."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from seedbox2.server.deps import TunebatDep, models, onboarding_pending

router = APIRouter(tags=["onboarding"])


@router.get("/onboarding")
def onboarding(request: Request) -> dict:
    return {"first_run": onboarding_pending(request.app)}


class Finished(BaseModel):
    done: bool = True  # false shows the flow again the next time, if the library is still empty


@router.post("/onboarding")
def finish(body: Finished, request: Request) -> dict:
    models(request.app).settings.set("onboarded", body.done)
    return {"first_run": onboarding_pending(request.app)}


@router.get("/tunebat/search")
def tunebat_search(q: str, tunebat: TunebatDep) -> dict:
    from seedbox2.library.tunebat_try import TunebatUnavailable

    if not q.strip():
        raise HTTPException(422, "type something to search for")
    try:
        return tunebat.search(q)
    except TunebatUnavailable:
        raise HTTPException(502, "Tunebat isn't answering. Check the connection and try again, or choose a folder of "
                                 "your own music instead.") from None  # fmt: skip


class Fetch(BaseModel):
    id: str


@router.post("/tunebat/download", status_code=202)
def tunebat_download(body: Fetch, tunebat: TunebatDep) -> dict:
    """Fetch one search result into the music folder. Progress: `tunebat.download` events."""
    from seedbox2.library.tunebat_try import UnknownTrack

    try:
        return tunebat.download(body.id)
    except UnknownTrack:
        raise HTTPException(404, "search for the track first") from None
