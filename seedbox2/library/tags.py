"""Read a file's tags with mutagen: MP3 (ID3), AAC/M4A (MP4 atoms), FLAC and OGG/Opus (Vorbis comments), WAV
and AIFF (ID3 chunks).

The tags are hints, not truth: the title, artist and album fill the track's columns (they are what the user
typed), but the BPM, key, comment, genre and year only go into `tracks.tags["file"]`. Our own analysis decides
the tempo and key (#43, #45); a tag may say 64 BPM for a 128 BPM track or a key nobody checked.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# the formats ffmpeg decodes for us (hashing.py) and mutagen reads tags from
AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".aac", ".mp4", ".alac", ".flac", ".wav", ".wave", ".aif", ".aiff",
                              ".aifc", ".ogg", ".oga", ".opus"})  # fmt: skip

_ARTWORK_EXT = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "image/webp": ".webp"}  # fmt: skip


def is_audio(path: Path | str) -> bool:
    p = Path(path)
    return p.suffix.lower() in AUDIO_EXTENSIONS and not p.name.startswith(".")


@dataclass
class FileTags:
    title: str = ""
    artist: str = ""
    album: str = ""
    hints: dict[str, Any] = field(default_factory=dict)  # bpm, key, comment, genre, year, track, ... (not truth)
    artwork: bytes | None = None
    artwork_mime: str = ""

    def save_artwork(self, folder: Path) -> Path | None:
        """Write the artwork once into `folder`, named by its own hash (the same cover is shared by an album)."""
        if not self.artwork:
            return None
        ext = _ARTWORK_EXT.get(self.artwork_mime.lower(), ".jpg")
        dest = folder / f"{hashlib.sha1(self.artwork).hexdigest()}{ext}"
        if not dest.exists():
            folder.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(self.artwork)
            tmp.replace(dest)
        return dest


def _first(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):  # also MP4's trkn: (number, total)
        v = v[0] if v else ""
    if hasattr(v, "text"):  # an ID3 frame
        return _first(v.text)
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    return str(v).strip().strip("\0")


def _bpm(v: str) -> float | None:
    try:
        bpm = float(v.replace(",", "."))
    except ValueError:
        return None
    return round(bpm, 2) if 0 < bpm < 1000 else None


def _id3(tags: Any) -> tuple[dict[str, str], tuple[bytes, str] | None]:
    out = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "bpm": "TBPM", "key": "TKEY", "genre": "TCON",
           "year": "TDRC", "track": "TRCK", "album_artist": "TPE2", "label": "TPUB", "remixer": "TPE4"}  # fmt: skip
    values = {k: _first(tags.get(frame)) for k, frame in out.items()}
    comments = [f for f in tags.getall("COMM") if _first(f.text)]
    if comments:
        values["comment"] = _first(comments[0].text)
    art = tags.getall("APIC")
    cover = next((a for a in art if a.type == 3), art[0] if art else None)
    return values, (cover.data, cover.mime) if cover is not None else None


def _mp4(tags: Any) -> tuple[dict[str, str], tuple[bytes, str] | None]:
    keys = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "genre": "\xa9gen", "year": "\xa9day",
            "comment": "\xa9cmt", "album_artist": "aART", "track": "trkn"}  # fmt: skip
    values = {k: _first(tags.get(atom)) for k, atom in keys.items()}
    if "tmpo" in tags:
        values["bpm"] = _first(tags["tmpo"])
    for name in ("----:com.apple.iTunes:initialkey", "----:com.apple.iTunes:KEY"):
        if name in tags:
            values["key"] = _first(tags[name])
    covers = tags.get("covr") or []
    if covers:
        from mutagen.mp4 import MP4Cover

        c = covers[0]
        return values, (bytes(c), "image/png" if c.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg")
    return values, None


def _vorbis(tags: Any, pictures: list) -> tuple[dict[str, str], tuple[bytes, str] | None]:
    keys = {"title": "title", "artist": "artist", "album": "album", "genre": "genre", "year": "date",
            "comment": "comment", "album_artist": "albumartist", "track": "tracknumber", "label": "label"}  # fmt: skip
    values = {k: _first(tags.get(v)) for k, v in keys.items()}
    values["bpm"] = _first(tags.get("bpm") or tags.get("tempo"))
    values["key"] = _first(tags.get("initialkey") or tags.get("key"))
    if not values["comment"]:
        values["comment"] = _first(tags.get("description"))
    if not pictures:  # OGG/Opus carry pictures base64-encoded in METADATA_BLOCK_PICTURE
        import base64

        from mutagen.flac import Picture

        for raw in tags.get("metadata_block_picture") or []:
            try:
                pictures.append(Picture(base64.b64decode(raw)))
            except Exception:  # noqa: BLE001, S112 - a damaged picture is no reason to skip the tags
                continue
    cover = next((p for p in pictures if p.type == 3), pictures[0] if pictures else None)
    return values, (cover.data, cover.mime) if cover is not None else None


def read_tags(path: Path | str) -> FileTags:
    """The file's tags; empty FileTags (never an error) when it has none or mutagen can't read it."""
    path = Path(path)
    try:
        import mutagen

        audio = mutagen.File(path)
    except Exception as e:  # noqa: BLE001 - mutagen raises many types for damaged files
        log.debug("no tags from %s: %s", path.name, e)
        audio = None
    values: dict[str, str] = {}
    art = None
    if audio is not None and audio.tags is not None:
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4Tags

        try:
            if isinstance(audio.tags, ID3):  # MP3, and the ID3 chunk of WAV and AIFF
                values, art = _id3(audio.tags)
            elif isinstance(audio.tags, MP4Tags):
                values, art = _mp4(audio.tags)
            else:  # VCommentDict and its FLAC/OGG/Opus kin
                values, art = _vorbis(audio.tags, list(getattr(audio, "pictures", []) or []))
        except Exception as e:  # noqa: BLE001
            log.debug("unreadable tags in %s: %s", path.name, e)
            values, art = {}, None
    hints: dict[str, Any] = {k: v for k, v in values.items() if k not in ("title", "artist", "album") and v}
    if "bpm" in hints:
        bpm = _bpm(hints.pop("bpm"))
        if bpm:
            hints["bpm"] = bpm
    info = getattr(audio, "info", None)
    for attr, name in (("bitrate", "bitrate"), ("sample_rate", "sample_rate"), ("channels", "channels")):
        v = getattr(info, attr, None)
        if isinstance(v, int) and v > 0:
            hints[name] = v
    hints["format"] = path.suffix.lower().lstrip(".")
    return FileTags(title=values.get("title", ""), artist=values.get("artist", ""), album=values.get("album", ""),
                    hints=hints, artwork=art[0] if art else None, artwork_mime=art[1] if art else "")  # fmt: skip
