"""/tracks/{hash}: per-track data the UI draws, as compact binary rather than JSON.

- `GET /tracks/{hash}/waveform` (#49): the `seedbox2.analysis.waveform` file, `application/octet-stream`. Sent
  gzipped (`Content-Encoding: gzip`, as stored) to clients that accept it. The ETag is the track's content hash, the
  format version and its stem count, so an unchanged waveform is a 304. Not computed yet: 202 with the queued job
  (at the front of the light queue); the `job.done` event says when to fetch again.
- `GET /tracks/{hash}/stems/{stem}` (#44): one stem's file (FLAC or Opus, with Range requests), which also counts as
  using the track's stems for the cache's eviction. 404 until they are separated.
- `GET /tracks/{hash}/audio` (#65): what the live engine can stream: `frames`, `chunk_frames`, and `sources`: `mix`, plus
  the four stems once they're separated.
- `GET /tracks/{hash}/audio/{source}/{index}`: chunk `index` of a source as FLAC at 44.1 kHz stereo, its samples exactly
  those of a whole decode from `chunk_frames * index` on: chunks butt up without a seam. 404 past the end, or for stems
  that aren't there.
- `GET /tracks/{hash}/artwork` (#73): the cover saved from the file's tags at import, for the library table.
"""

from __future__ import annotations

import gzip
import math
import subprocess
import zlib
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

from musicreco.paths import ffmpeg_path
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


@router.get("/{hash_}/waveform/stems", response_class=Response,
            responses={200: {"content": {"application/octet-stream": {}}}, 202: {}, 304: {}, 404: {}})  # fmt: skip
def track_stem_waveform(hash_: str, request: Request, lib: LibraryDep) -> Response:
    """The stem detail file (#78). 404 until the track has stems; 202 while the waveform job writes it. Stems can be
    separated again, so the ETag goes by the file's time."""
    if lib.track(hash_) is None:
        raise HTTPException(404, "no such track")
    path = waveform.current_stem_artifact(lib, hash_)
    if path is None:
        if stems.current(lib, hash_) is None:
            raise HTTPException(404, "no stems yet")
        job = waveform.submit(get_jobs(request), hash_, front=True)
        return JSONResponse({"state": job["state"], "job": job}, status_code=202)
    etag = f'"{hash_}.stems.v{waveform.STEM_VERSION}.{path.stat().st_mtime_ns}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
    if etag in [t.strip().removeprefix("W/") for t in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    data = path.read_bytes()
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


CHUNK_SAMPLE_RATE = 44100
CHUNK_FRAMES = 20 * CHUNK_SAMPLE_RATE
_PRE_ROLL = CHUNK_SAMPLE_RATE // 4  # samples decoded and thrown away after a seek: decoders settle on real history
_CHUNK_TIMEOUT_S = 120


def _chunk_flac(path: Path, index: int, *, seek: bool) -> bytes:
    """One chunk of a file, sample-exact against a whole decode. Only FLAC at 44.1 kHz (the stems) lands exactly on a
    seek: AAC and Opus seek off by hundreds of samples, and a resampler's first samples aren't a whole decode's. The rest
    decode from the start, which costs about a second at the end of a long track."""
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise HTTPException(503, "ffmpeg isn't installed")
    start = index * CHUNK_FRAMES
    skip = _PRE_ROLL if seek and start else start
    args = [ffmpeg, "-v", "error", "-nostdin"]
    if seek and start:
        args += ["-ss", f"{(start - skip) / CHUNK_SAMPLE_RATE:.6f}"]
    args += ["-i", str(path), "-vn", "-af",
             f"aresample={CHUNK_SAMPLE_RATE},atrim=start_sample={skip}:end_sample={skip + CHUNK_FRAMES},"
             "asetpts=PTS-STARTPTS",
             "-ac", "2", "-c:a", "flac", "-sample_fmt", "s32", "-compression_level", "0", "-f", "flac", "pipe:1"]  # fmt: skip
    try:
        r = subprocess.run(args, capture_output=True, timeout=_CHUNK_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as e:
        raise HTTPException(504, "decoding took too long") from e
    if r.returncode != 0:
        raise HTTPException(500, f"couldn't decode the audio: {r.stderr.decode(errors='replace')[-300:]}")
    return r.stdout


def _track_file(lib: LibraryDep, hash_: str) -> tuple[dict, Path]:
    track = lib.track(hash_)
    if track is None:
        raise HTTPException(404, "no such track")
    for p in track["paths"]:
        if Path(p).is_file():
            return track, Path(p)
    raise HTTPException(404, "the track's file isn't there")


@router.get("/{hash_}/audio")
def track_audio(hash_: str, lib: LibraryDep) -> dict:
    track, _ = _track_file(lib, hash_)
    folder = stems.current(lib, hash_)
    files = waveform.stem_files(folder) if folder is not None else None
    return {
        "frames": math.ceil(float(track["duration"] or 0.0) * CHUNK_SAMPLE_RATE),
        "chunk_frames": CHUNK_FRAMES,
        "sample_rate": CHUNK_SAMPLE_RATE,
        "sources": ["mix", *(files or ())],
    }


@router.get("/{hash_}/audio/{source}/{index}", response_class=Response,
            responses={200: {"content": {"audio/flac": {}}}, 404: {}})  # fmt: skip
def track_audio_chunk(hash_: str, source: str, index: int, lib: LibraryDep) -> Response:
    if index < 0:
        raise HTTPException(404, "no such chunk")
    track, path = _track_file(lib, hash_)
    if source == "mix":
        seek = False
    else:
        folder = stems.current(lib, hash_)
        files = waveform.stem_files(folder) if folder is not None else None
        if not files or source not in files:
            raise HTTPException(404, "no such stem yet")
        stems.touch(folder)  # type: ignore[arg-type]
        path = files[source]
        seek = path.suffix == ".flac"
    if index * CHUNK_FRAMES >= math.ceil(float(track["duration"] or 0.0) * CHUNK_SAMPLE_RATE):
        raise HTTPException(404, "no such chunk")
    return Response(_chunk_flac(path, index, seek=seek), media_type="audio/flac", headers={"Cache-Control": "no-cache"})


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
