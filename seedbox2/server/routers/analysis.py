"""/analysis: beats, key, stems, sections, waveforms per track (#43–#49).

- `GET /analysis/{hash}/beats` (#43): the track's `beats` analysis (`seedbox2.analysis.beats`): the piecewise grid
  `segments: [{start_beat, start_s, bpm}]`, the main BPM, the time signature check and how it was made. Not computed
  yet: 202 with the queued job (at the front of the light queue); the `job.done` event says when to fetch again.
"""

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse

from seedbox2.analysis import beats
from seedbox2.server.deps import LibraryDep, get_jobs

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
