"""/tunebat/crates: Seedbox 1's search → crate → download flow (`seedbox2/library/tunebat_crate.py`). The search is the
first run's `GET /tunebat/search`.

- `GET /tunebat/crates/{seed_id}`: the seed and every track tunebat recommends from it, best first, each `related` if it
  passes Seedbox 1's pre-tick rule. 502 if tunebat doesn't answer.
- `POST /tunebat/crates/{seed_id}/download` `{ids}`: download those (the seed's id for the seed) into the crate's folder
  and import it as a crate. 404 if the crate wasn't opened or an id isn't in it.
- `GET /tunebat/crates/downloads`, `POST /tunebat/crates/downloads/{id}/cancel`.

Events: `tunebat.crate`, the whole download, on every change.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from seedbox2.server.deps import TunebatCratesDep

router = APIRouter(prefix="/tunebat/crates", tags=["tunebat"])


@router.get("/downloads")
def downloads(crates: TunebatCratesDep) -> list[dict]:
    return crates.jobs()


@router.post("/downloads/{job_id}/cancel")
def cancel(job_id: str, crates: TunebatCratesDep) -> dict:
    job = crates.cancel(job_id)
    if job is None:
        raise HTTPException(404, "no such download")
    return job


@router.get("/{seed_id}")
def open_crate(seed_id: str, crates: TunebatCratesDep) -> dict:
    from seedbox2.library.tunebat_try import TunebatUnavailable

    try:
        return crates.open(seed_id)
    except TunebatUnavailable:
        raise HTTPException(502, "Tunebat isn't answering. Check the connection and try again.") from None


class Download(BaseModel):
    ids: list[str]


@router.post("/{seed_id}/download", status_code=202)
def download(seed_id: str, body: Download, crates: TunebatCratesDep) -> dict:
    from seedbox2.library.tunebat_crate import UnknownCrate

    try:
        return crates.download(seed_id, body.ids)
    except UnknownCrate:
        raise HTTPException(404, "open the crate first: that isn't one of its tracks") from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
