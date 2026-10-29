"""/tracks/{hash}: per-track data the UI draws, as compact binary rather than JSON.

- `GET /tracks/{hash}/waveform` (#49): the `seedbox2.analysis.waveform` file, `application/octet-stream`. Sent
  gzipped (`Content-Encoding: gzip`, as stored) to clients that accept it. The ETag is the track's content hash, the
  format version and its stem count, so an unchanged waveform is a 304. Not computed yet: 202 with the queued job
  (at the front of the light queue); the `job.done` event says when to fetch again.
"""

from __future__ import annotations

import gzip
import zlib

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse, Response

from seedbox2.analysis import waveform
from seedbox2.server.deps import LibraryDep, get_jobs

router = APIRouter(prefix="/tracks", tags=["tracks"])


@router.get("/{hash_}/waveform", response_class=Response,
            responses={200: {"content": {"application/octet-stream": {}}}, 202: {}, 304: {}, 404: {}})  # fmt: skip
def track_waveform(hash_: str, request: Request, lib: LibraryDep) -> Response:
    if lib.track(hash_) is None:
        raise HTTPException(404, "no such track")
    path = waveform.current_artifact(lib, hash_)
    if path is None:
        job = waveform.submit(get_jobs(request), hash_, front=True)
        return JSONResponse({"state": job["state"], "job": job}, status_code=202)
    data = path.read_bytes()
    try:
        header = waveform.read_header(zlib.decompressobj(wbits=31).decompress(data, waveform.HEADER_SIZE))
    except (zlib.error, waveform.WaveformError) as e:
        raise HTTPException(500, f"damaged waveform file: {e}") from e
    etag = f'"{hash_}.v{header["version"]}.s{header["stems"]}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
    if etag in [t.strip().removeprefix("W/") for t in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(data, media_type="application/octet-stream", headers={**headers, "Content-Encoding": "gzip"})
    return Response(gzip.decompress(data), media_type="application/octet-stream", headers=headers)
