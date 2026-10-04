"""Crate folders on disk, shared by both apps: path containment, audio / cover URLs, embedded art, the crate's
tagged files. Setbox's set-building operations on them live in setbox/crates.py."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mutagen.id3 import ID3

from musicreco import library
from musicreco.models import Track

from . import serializers

log = logging.getLogger(__name__)

AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".aif", ".aiff", ".flac", ".ogg", ".opus"}


class ApiError(Exception):
    """An error with an HTTP status; the server turns it into `{detail}`."""

    status = 400

    def __init__(self, detail: str, status: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        if status is not None:
            self.status = status


class NotFound(ApiError):
    status = 404


class Conflict(ApiError):
    status = 409


# --- path containment ---------------------------------------------------------------------------------------------


def _single_name(name: str, what: str) -> None:
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise ApiError(f"invalid {what} name")


def inside(path: Path, root: Path) -> bool:
    """True if `path` (symlinks resolved) is `root` or below it."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def resolve_crate(root: Path, folder: str) -> Path:
    """The crate folder `root/folder`, refusing anything that resolves outside root."""
    _single_name(folder, "crate folder")
    path = root / folder
    if not inside(path, root) or path.resolve() == root.resolve():
        raise ApiError("crate folder outside the crates root", 403)
    if not path.is_dir():
        raise NotFound(f"no crate folder named {folder!r}")
    return path


def resolve_crate_file(root: Path, folder: str, filename: str) -> Path:
    """An audio file directly inside a crate folder."""
    crate = resolve_crate(root, folder)
    _single_name(filename, "file")
    path = crate / filename
    if filename.startswith(".") or path.suffix.lower() not in AUDIO_SUFFIXES:
        raise NotFound("no such audio file")
    if not inside(path, crate) or path.resolve().parent != crate.resolve() or not path.is_file():
        raise NotFound("no such audio file")
    return path


def audio_url(folder: str, filename: str) -> str:
    return f"/api/files/{quote(folder, safe='')}/{quote(filename, safe='')}"


# --- embedded cover art --------------------------------------------------------------------------------------------

_art_lock = threading.Lock()
_has_art: dict[str, tuple[tuple[int, int], bool]] = {}  # path -> ((size, mtime_ns), has APIC)


def embedded_art(path: Path) -> tuple[str, bytes] | None:
    """(mime, bytes) of the MP3's APIC art (front cover preferred), or None."""
    try:
        frames = ID3(path).getall("APIC")
    except Exception:  # noqa: BLE001 - no tag / unreadable
        return None
    frames = [f for f in frames if f.data]
    if not frames:
        return None
    best = next((f for f in frames if f.type == 3), frames[0])
    mime = (best.mime or "").lower()
    if not mime.startswith("image/"):
        mime = "image/png" if best.data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return mime, best.data


def has_art(path: Path) -> bool:
    """Whether the file carries embedded art (cached until the file changes)."""
    try:
        st = path.stat()
    except OSError:
        return False
    sig = (st.st_size, st.st_mtime_ns)
    key = str(path)
    with _art_lock:
        hit = _has_art.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    found = embedded_art(path) is not None
    with _art_lock:
        if len(_has_art) > 20_000:
            _has_art.clear()
        _has_art[key] = (sig, found)
    return found


def cover_url(folder: str, track_id: str) -> str:
    return f"/api/covers/{quote(folder, safe='')}/{quote(track_id, safe='')}"


def with_cover(track: dict | None, folder: str, path: Path | None) -> dict | None:
    """Track JSON whose missing cover_url points at the file's embedded art (old crates rebuilt from tracks.csv)."""
    if track is None or track.get("cover_url") or path is None or not has_art(path):
        return track
    return {**track, "cover_url": cover_url(folder, track["id"])}


def track_json(t: Any) -> Any:
    """A Track (or raw tunebat dict) as Track JSON; JSON-shaped dicts and None pass through."""
    if isinstance(t, Track):
        return serializers.track(t)
    if isinstance(t, dict) and "display_name" not in t and "id" in t and "n" in t:
        return serializers.track(Track.from_api(t))
    return t


# --- the crate's files ---------------------------------------------------------------------------------------------


@dataclass
class SetFile:
    """One MP3 of the set: its file, its Track and the tag-derived extras."""

    path: Path
    track: Track
    energy_rating: int | None
    genre: str | None


def _signature(seed_dir: Path) -> tuple:
    """Cheap fingerprint of a crate folder: MP3 names/sizes/mtimes + the metadata files."""
    sig = []
    try:
        for p in sorted(seed_dir.iterdir()):
            if p.suffix.lower() == ".mp3" or p.name == "tracks.csv":
                st = p.stat()
                sig.append((p.name, st.st_size, st.st_mtime_ns))
        cj = library.crate_json_path(seed_dir)
        if cj.is_file():
            st = cj.stat()
            sig.append((".crate.json", st.st_size, st.st_mtime_ns))
    except OSError:
        pass
    return tuple(sig)


def scan_crate(seed_dir: Path) -> list[SetFile]:
    """The crate's MP3s that carry a Spotify ID, in set order (library.crate_files: TRCK, unnumbered last)."""
    known = library.load_crate_tracks(seed_dir)
    out: list[SetFile] = []
    seen: set[str] = set()
    for f in library.crate_files(seed_dir):
        tid = f.track_id
        if not tid or tid in seen or tid not in known:  # hand-dropped files without an ID can't be placed
            continue
        seen.add(tid)
        out.append(SetFile(f.path, known[tid], f.energy_rating, f.genre or None))
    return out


def file_of(seed_dir: Path, track_id: str) -> Path | None:
    """The MP3 directly in the crate folder tagged with this Spotify ID (ID reads are cached by mtime)."""
    found = library.LibraryIndex(seed_dir).find(track_id)
    return next((p for p in found if p.parent == seed_dir), None)


def crate_cover(root: Path, folder: str, track_id: str) -> tuple[Path, str, bytes]:
    """(file, mime, bytes) of a crate track's embedded art; NotFound if the track or its art is missing."""
    seed_dir = resolve_crate(root, folder)
    path = file_of(seed_dir, track_id)
    art = embedded_art(path) if path is not None else None
    if path is None or art is None:
        raise NotFound("no embedded cover art for this track")
    return path, *art


def summary_seed(seed_dir: Path, seed: dict | None) -> dict | None:
    """CrateSummary.seed with cover_url filled from the seed file's art when tunebat's is unknown."""
    if seed is None or seed.get("cover_url"):
        return seed
    return with_cover(seed, seed_dir.name, file_of(seed_dir, seed["id"]))
