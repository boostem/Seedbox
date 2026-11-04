"""/tracks/{hash}: per-track data the UI draws, as compact binary rather than JSON.

- `GET /tracks/{hash}/waveform` (#49): the `seedbox2.analysis.waveform` file, `application/octet-stream`. Sent
  gzipped (`Content-Encoding: gzip`, as stored) to clients that accept it. The ETag is the track's content hash, the
  format version and its stem count, so an unchanged waveform is a 304. Not computed yet: 202 with the queued job
  (at the front of the light queue); the `job.done` event says when to fetch again.
- `GET /tracks/{hash}/stems/{stem}` (#44): one stem's file (FLAC or Opus, with Range requests), which also counts as
  using the track's stems for the cache's eviction. 404 until they are separated.
- `GET /tracks/{hash}/artwork` (#73): the cover saved from the file's tags at import, for the library table.
"""

from __future__ import annotations

import gzip
import zlib
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

from seedbox2.analysis import stems, waveform
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


_STEM_TYPES = {".flac": "audio/flac", ".opus": "audio/ogg"}


@router.get("/{hash_}/stems/{stem}", response_class=Response, responses={200: {"content": {"audio/*": {}}}, 404: {}})
def track_stem(hash_: str, stem: str, lib: LibraryDep) -> Response:
    folder = stems.current(lib, hash_)
    files = waveform.stem_files(folder) if folder is not None else None
    if not files or stem not in files:
        raise HTTPException(404, "no such stem yet")
    stems.touch(folder)  # type: ignore[arg-type]
    return FileResponse(files[stem], media_type=_STEM_TYPES.get(files[stem].suffix, "application/octet-stream"),
                        headers={"Cache-Control": "no-cache"})  # fmt: skip


_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}


@router.get("/{hash_}/artwork", response_class=Response, responses={200: {"content": {"image/*": {}}}, 404: {}})
def track_artwork(hash_: str, lib: LibraryDep) -> Response:
    """The track's cover: the image its file carried (saved at import), else a redirect to the cover tunebat gave
    Seedbox 1. 404 if it has none. Covers are named by their own hash, so they are cached for a day."""
    t = lib.track(hash_)
    if t is None:
        raise HTTPException(404, "no such track")
    tags = t["tags"] or {}
    art = tags.get("artwork")
    if isinstance(art, str) and (path := Path(art)).suffix.lower() in _IMAGE_TYPES and path.is_file():
        return FileResponse(path, media_type=_IMAGE_TYPES[path.suffix.lower()], headers={"Cache-Control": "max-age=86400"})
    url = (tags.get("tunebat") or {}).get("cover_url") if isinstance(tags.get("tunebat"), dict) else None
    if isinstance(url, str) and url.startswith("https://"):
        return RedirectResponse(url, status_code=302)
    raise HTTPException(404, "no artwork")
