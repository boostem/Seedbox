"""/analysis: beats, key, stems, sections, waveforms per track (#43–#49).

- `GET /analysis/{hash}/beats` (#43): the track's `beats` analysis (`seedbox2.analysis.beats`): the piecewise grid
  `segments: [{start_beat, start_s, bpm}]`, the main BPM, the time signature check and how it was made. Not computed
  yet: 202 with the queued job (at the front of the light queue); the `job.done` event says when to fetch again.
- `GET /analysis/stems`: the stems cache: its settings, what it holds and which model low-power mode would use.
- `POST /analysis/stems/settings`: `{limit_gb, format, low_power}`; a lower limit evicts at once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from seedbox2.analysis import beats, stems
from seedbox2.library.service import SETTINGS_FILE, Settings
from seedbox2.server.deps import LibraryDep, get_jobs, get_library

router = APIRouter(prefix="/analysis", tags=["analysis"])


@router.get("/{hash_}/beats", responses={200: {}, 202: {}, 404: {}}, response_model=None)
def track_beats(hash_: str, request: Request, lib: LibraryDep) -> dict | JSONResponse:
    if lib.track(hash_) is None:
        raise HTTPException(404, "no such track")
    row = lib.analysis(hash_, beats.ANALYSER, beats.VERSION)
    if row is None:
        job = beats.submit(get_jobs(request), hash_, front=True)
        return JSONResponse({"state": job["state"], "job": job}, status_code=202)
    return row["data"]


def _settings(request: Request) -> Settings:
    return Settings(Path(get_library(request).db.path).parent / SETTINGS_FILE)


def _status(settings: Settings) -> dict:
    return {**stems.read_settings(settings), "usage": stems.usage()}


class StemsSettings(BaseModel):
    limit_gb: float | None = Field(default=None, ge=0, le=100_000)
    format: Literal["flac", "opus"] | None = None
    low_power: bool | None = None


@router.get("/stems")
def stems_status(settings: Settings = Depends(_settings)) -> dict:  # noqa: B008
    return _status(settings)


@router.post("/stems/settings")
def stems_settings(body: StemsSettings, lib: LibraryDep, settings: Settings = Depends(_settings)) -> dict:  # noqa: B008
    current = stems.read_settings(settings)
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    new = {**current, **changes}
    settings.set(stems.SETTINGS_KEY, new)
    if new["limit_gb"] < current["limit_gb"]:
        stems.evict(lib, int(new["limit_gb"] * stems.GB))
    return _status(settings)
