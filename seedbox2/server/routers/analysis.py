"""/analysis: beats, key, stems, sections, waveforms per track (#43–#49).

- `GET /analysis/{hash}/beats` (#43): the track's `beats` analysis (`seedbox2.analysis.beats`): the piecewise grid
  `segments: [{start_beat, start_s, bpm}]`, the main BPM, the time signature check and how it was made. Not computed
  yet: 202 with the queued job (at the front of the light queue); the `job.done` event says when to fetch again.
- `GET /analysis/{hash}/key` (#45): `{analysis, effective, hints}` (`seedbox2.analysis.key.status`). Not computed yet: 202
  as above. `PUT /analysis/{hash}/key/override` with `{"key": "8A"}` (any notation) sets the user's key, `DELETE` goes
  back to ours; both answer with the status.
- `GET /analysis/{hash}/vocals` (#47): per-beat vocal activity, phrases and windows (`seedbox2.analysis.vocals`). Not
  computed yet: 202, with the queued job once the track has stems and beats, else `{"state": "waiting"}`.
- `GET /analysis/{hash}/sections` (#46): the track's `sections` analysis (`seedbox2.analysis.sections`): sections,
  phrases, mood, fill-ins, mix-in and mix-out candidates. 202 with the queued job if it hasn't been computed yet.
- `GET /analysis/stems`: the stems cache: its settings, what it holds and which model low-power mode would use.
- `POST /analysis/stems/settings`: `{limit_gb, format, low_power}`; a lower limit evicts at once.
- `GET /analysis/{hash}/energy` (#48): per-bar energy 0-1 and the track's 1-10. 202 while its job is queued.
- `GET /analysis/{hash}/loudness`: the 400 ms LUFS curve, the integrated LUFS and true peak, and both per section, and the gain for playing the track (`track_gain_db`, #58; the
  sections analysis, or the energy curve's intro, main and outro until there is one). 202 while its job is queued.
- `GET /analysis/{hash}/vibe`: which windows were embedded. 202 while its job is queued, 503 without the model.
- `GET /analysis/{hash}/similar?limit&window&bpm&key&bpm_pct`: the nearest tracks by vibe, compatible with this track's
  BPM and key (`bpm=false` or `key=false` turn a filter off).
- `POST /analysis/vibe/search`: `{text, limit, bpm, bpm_pct, key}`: the tracks nearest to a text like "warm", filtered
  when `bpm` or `key` (Camelot) is given. 503 without the models.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from seedbox2.analysis import beats, clap, energy, key, loudness, sections, stems, vibe, vocals
from seedbox2.library.service import SETTINGS_FILE, Settings
from seedbox2.library.view import to_camelot
from seedbox2.server.deps import LibraryDep, get_jobs, get_library, vibe_index

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


@router.get("/{hash_}/key", responses={200: {}, 202: {}, 404: {}}, response_model=None)
def track_key(hash_: str, request: Request, lib: LibraryDep) -> dict | JSONResponse:
    if lib.track(hash_) is None:
        raise HTTPException(404, "no such track")
    if lib.analysis(hash_, key.ANALYSER, key.VERSION) is None:
        job = key.submit(get_jobs(request), hash_, front=True)
        return JSONResponse({"state": job["state"], "job": job}, status_code=202)
    return key.status(lib, hash_)


class KeyOverride(BaseModel):
    key: str


@router.put("/{hash_}/key/override")
def set_key_override(hash_: str, body: KeyOverride, lib: LibraryDep) -> dict:
    if lib.track(hash_) is None:
        raise HTTPException(404, "no such track")
    try:
        key.set_override(lib, hash_, body.key)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return key.status(lib, hash_)


@router.delete("/{hash_}/key/override")
def clear_key_override(hash_: str, lib: LibraryDep) -> dict:
    if lib.track(hash_) is None:
        raise HTTPException(404, "no such track")
    key.clear_override(lib, hash_)
    return key.status(lib, hash_)
@router.get("/{hash_}/vocals", responses={200: {}, 202: {}, 404: {}}, response_model=None)
def track_vocals(hash_: str, request: Request, lib: LibraryDep) -> dict | JSONResponse:
    if lib.track(hash_) is None:
        raise HTTPException(404, "no such track")
    row = lib.analysis(hash_, vocals.ANALYSER, vocals.VERSION)
    if row is not None:
        return row["data"]
    if stems.current(lib, hash_) is None or lib.analysis(hash_, beats.ANALYSER) is None:
        return JSONResponse({"state": "waiting"}, status_code=202)
    job = vocals.submit(get_jobs(request), hash_, front=True)
    return JSONResponse({"state": job["state"], "job": job}, status_code=202)


@router.get("/{hash_}/sections", responses={200: {}, 202: {}, 404: {}}, response_model=None)
def track_sections(hash_: str, request: Request, lib: LibraryDep) -> dict | JSONResponse:
    if lib.track(hash_) is None:
        raise HTTPException(404, "no such track")
    row = lib.analysis(hash_, sections.ANALYSER, sections.VERSION)
    if row is None:
        job = sections.submit(get_jobs(request), hash_, front=True)
        return JSONResponse({"state": job["state"], "job": job}, status_code=202)
    return row["data"]


def _energy_row(hash_: str, analyser: str, request: Request, lib: Any) -> dict | JSONResponse:
    if lib.track(hash_) is None:
        raise HTTPException(404, "no such track")
    row = lib.analysis(hash_, analyser, energy.VERSION)
    if row is None:
        job = energy.submit(get_jobs(request), hash_, front=True)
        return JSONResponse({"state": job["state"], "job": job}, status_code=202)
    return row["data"]


@router.get("/{hash_}/energy", responses={200: {}, 202: {}, 404: {}}, response_model=None)
def track_energy(hash_: str, request: Request, lib: LibraryDep) -> dict | JSONResponse:
    return _energy_row(hash_, energy.ANALYSER, request, lib)


@router.get("/{hash_}/loudness", responses={200: {}, 202: {}, 404: {}}, response_model=None)
def track_loudness(hash_: str, request: Request, lib: LibraryDep) -> dict | JSONResponse:
    data = _energy_row(hash_, energy.LOUDNESS_ANALYSER, request, lib)
    if isinstance(data, JSONResponse):
        return data
    row = lib.analysis(hash_, energy.ANALYSER, energy.VERSION)
    spans = vibe.spans_of(lib, hash_, row["data"] if row else None)
    return {**data, "sections": loudness.sections(data, spans), "track_gain_db": loudness.track_gain_db(data, spans)}


@router.get("/{hash_}/vibe", responses={200: {}, 202: {}, 404: {}, 503: {}}, response_model=None)
def track_vibe(hash_: str, request: Request, lib: LibraryDep) -> dict | JSONResponse:
    if lib.track(hash_) is None:
        raise HTTPException(404, "no such track")
    path = vibe.current(lib, hash_)
    if path is not None:
        data = vibe.read_embeddings(path)
        return {"model": vibe.model_version(), "windows": [{"name": n, "start_s": s, "seconds": clap.WINDOW_S}
                                                           for n, s in zip(data["names"], data["starts"], strict=True)]}  # fmt: skip
    if not vibe.audio_ready():
        raise HTTPException(503, "the CLAP model isn't downloaded")
    jobs = get_jobs(request)
    job = (vibe.submit(jobs, hash_, priority="set") if lib.analysis(hash_, energy.ANALYSER, energy.VERSION)
           else energy.submit(jobs, hash_, front=True))  # fmt: skip
    return JSONResponse({"state": job["state"], "job": job}, status_code=202)


def _index(request: Request) -> vibe.Index:
    return vibe_index(request)


@router.get("/{hash_}/similar", responses={200: {}, 404: {}, 409: {}}, response_model=None)
def track_similar(hash_: str, request: Request, lib: LibraryDep, limit: int = Query(default=20, ge=1, le=200),
                  window: Literal["intro", "main", "outro", "breakdown", "second"] | None = None, bpm: bool = True,
                  key: bool = True, bpm_pct: float = Query(default=vibe.DEFAULT_BPM_PCT, gt=0, le=50)) -> dict:  # fmt: skip
    if lib.track(hash_) is None:
        raise HTTPException(404, "no such track")
    try:
        out = vibe.similar(lib, _index(request), hash_, window=window, use_bpm=bpm, use_key=key, bpm_pct=bpm_pct, limit=limit)
    except vibe.VibeError as e:
        raise HTTPException(409, str(e)) from e
    if out is None:
        raise HTTPException(409, "the track has no embeddings yet")
    return out


class VibeSearch(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=20, ge=1, le=200)
    bpm: float | None = Field(default=None, gt=0, le=400)
    bpm_pct: float = Field(default=vibe.DEFAULT_BPM_PCT, gt=0, le=50)
    key: str | None = None


@router.post("/vibe/search", responses={200: {}, 422: {}, 503: {}})
def vibe_search(body: VibeSearch, request: Request, lib: LibraryDep) -> dict:
    camelot = None
    if body.key is not None and (camelot := to_camelot(body.key)) is None:
        raise HTTPException(422, f"not a key: {body.key}")
    try:
        return vibe.search(get_jobs(request), lib, _index(request), body.text, bpm=body.bpm, bpm_pct=body.bpm_pct,
                           camelot=camelot, limit=body.limit)  # fmt: skip
    except vibe.ModelNotReady as e:
        raise HTTPException(503, str(e)) from e
    except vibe.VibeError as e:
        raise HTTPException(422 if "empty" in str(e) else 503, str(e)) from e


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
