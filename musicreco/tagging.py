"""ID3v2.3 tagging for downloaded MP3s (mutagen). Thread-safe; cover art is cached by URL."""

from __future__ import annotations

import logging
import re
import string
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import requests
from mutagen import id3

from .models import BeatportInfo, Loudness, Track

log = logging.getLogger(__name__)

SPOTIFY_ID_KEY = "TXXX:SPOTIFY_TRACK_ID"
COVER_TIMEOUT_S = 15
COVER_TRIES = 3

# POPM rating bytes for 1..5 stars: the Windows/WMP/Serato convention vs Traktor's 51 steps.
_POPM_STD = (1, 64, 128, 196, 255)
_POPM_TRAKTOR = (51, 102, 153, 204, 255)
# (POPM owner "email", bytes per star). Order matters for reading: the first rated frame wins.
_POPM_PROFILES: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("Windows Media Player 9 Series", _POPM_STD),
    ("", _POPM_STD),
    ("Serato", _POPM_STD),
    ("traktor@native-instruments.de", _POPM_TRAKTOR),
)
_WINDOWS_READ_RANGES = (31, 95, 159, 223, 255)  # upper byte bound of 1..5 stars when Windows reads POPM

_cover_cache: dict[str, tuple[str, bytes]] = {}  # url -> (mime, bytes)
_cover_lock = threading.Lock()
_edit_lock = threading.Lock()  # serialises read-modify-write edits of existing files


def _enc(text: str) -> int:
    """Latin-1 for plain ASCII, UTF-16 (the safe v2.3 choice) for everything else."""
    return 0 if text.isascii() else 1


def _save_v23(tags: id3.ID3, path: Path) -> None:
    """Save as ID3v2.3 (TDRC -> TYER etc.) and drop any ID3v1 tag."""
    tags.update_to_v23()
    tags.save(path, v1=0, v2_version=3)


def _sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def _fetch_cover(url: str | None) -> tuple[str, bytes] | None:
    """Download cover art (cached by URL). Returns (mime, bytes), or None on failure."""
    if not url:
        return None
    with _cover_lock:
        cached = _cover_cache.get(url)
    if cached:
        return cached

    for attempt in range(1, COVER_TRIES + 1):
        try:
            resp = requests.get(url, timeout=COVER_TIMEOUT_S)
            if resp.status_code == 200 and resp.content:
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                mime = ctype if ctype.startswith("image/") else _sniff_mime(resp.content)
                if mime:
                    cover = (mime, resp.content)
                    with _cover_lock:
                        _cover_cache[url] = cover
                    return cover
                log.warning("cover %s: not an image (Content-Type %r)", url, ctype)
                return None
            if resp.status_code < 500 and resp.status_code != 429:
                log.warning("cover %s: HTTP %s", url, resp.status_code)
                return None
            err: object = f"HTTP {resp.status_code}"
        except requests.RequestException as e:
            err = e
        if attempt < COVER_TRIES:
            time.sleep(0.5 * attempt)
        else:
            log.warning("cover %s: giving up after %d tries (%s)", url, COVER_TRIES, err)
    return None


def prefetch_cover(url: str | None) -> None:
    """Warm the cover cache ahead of write_tags. Safe from any thread; never raises."""
    try:
        _fetch_cover(url)
    except Exception as e:  # noqa: BLE001 - write_tags simply fetches it again
        log.warning("cover prefetch %s failed: %s", url, e)


def _ascii_url(url: str) -> str:
    """WOAF is Latin-1 only: percent-encode anything outside printable ASCII."""
    return quote(url.strip(), safe=string.punctuation)


def write_tags(
    path: Path,
    track: Track,
    seed: Track,
    genre: str | None,
    beatport: BeatportInfo | None = None,
    loudness: Loudness | None = None,
) -> None:
    """Replace all tags on `path` with the DJ tag set (ID3v2.3). Cover failure is non-fatal.

    `beatport` adds label/ISRC/mix/remixers/URL/IDs and `loudness` adds ReplayGain; key and BPM always stay tunebat's.
    """
    tags = id3.ID3()  # empty: saving it over the file drops yt-dlp/YouTube leftovers

    def text(frame: type[id3.TextFrame], value: str) -> None:
        tags.add(frame(encoding=_enc(value), text=[value]))

    def extra(desc: str, value: str) -> None:
        tags.add(id3.TXXX(encoding=_enc(value), desc=desc, text=[value]))

    text(id3.TIT2, track.name)
    text(id3.TPE1, track.artist_str)
    text(id3.TALB, track.album)
    if track.year:
        text(id3.TDRC, track.year)  # becomes TYER via update_to_v23
    if track.bpm and round(track.bpm) > 0:
        text(id3.TBPM, str(round(track.bpm)))
    if track.camelot:
        text(id3.TKEY, track.camelot)
    text(id3.TIT1, seed.display_name)
    if genre:
        text(id3.TCON, genre)
    tags.add(id3.TXXX(encoding=0, desc="SPOTIFY_TRACK_ID", text=[track.id]))

    if beatport:
        if label := (beatport.label or "").strip():
            text(id3.TPUB, label)
        if isrc := (beatport.isrc or "").strip():
            text(id3.TSRC, isrc)
        if mix_name := (beatport.mix_name or "").strip():
            text(id3.TIT3, mix_name)
        if remixers := ", ".join(r.strip() for r in beatport.remixers if r and r.strip()):
            text(id3.TPE4, remixers)
        if url := (beatport.url or "").strip():
            tags.add(id3.WOAF(url=_ascii_url(url)))
        if beatport.track_id:
            extra("BEATPORT_TRACK_ID", str(beatport.track_id))
        if catalog_number := (beatport.catalog_number or "").strip():
            extra("CATALOGNUMBER", catalog_number)

    if loudness:
        extra("REPLAYGAIN_TRACK_GAIN", f"{round(loudness.replaygain_db, 2) + 0.0:.2f} dB")  # + 0.0 avoids "-0.00"
        extra("REPLAYGAIN_TRACK_PEAK", f"{loudness.peak:.6f}")

    cover = _fetch_cover(track.cover_url)
    if cover:
        mime, data = cover
        tags.add(id3.APIC(encoding=0, mime=mime, type=3, desc="", data=data))

    _save_v23(tags, path)


@contextmanager
def _editing(path: Path) -> Iterator[id3.ID3]:
    """Load `path`'s tags (empty if none), yield them for in-place edits, save as v2.3 unless the body raised."""
    with _edit_lock:
        try:
            tags = id3.ID3(path)
        except id3.ID3NoHeaderError:
            tags = id3.ID3()
        yield tags
        _save_v23(tags, path)


def set_grouping(path: Path, seed: Track) -> None:
    """Replace only the Grouping (TIT1) frame; all other tags are left as they are."""
    with _editing(path) as tags:
        tags.setall("TIT1", [id3.TIT1(encoding=_enc(seed.display_name), text=[seed.display_name])])


def _energy_to_stars(rating: int) -> int:
    """1-10 -> 1-5 stars: halve, round half up, clamp."""
    return min(5, max(1, (int(rating) + 1) // 2))


def set_energy(path: Path, rating: int) -> None:
    """Write the crate-relative energy (1-10) as a 1-5 star rating in POPM frames; nothing else is touched.

    stars = round-half-up(rating / 2), clamped to 1-5. POPM rating bytes are not standardised, so one frame is
    written per reader family (ID3 allows several POPM frames with different owner "emails"; mutagen saves the
    shortest owner first, so a "first POPM wins" reader gets the Windows-style bytes). Research, 2026-10:
    - "Windows Media Player 9 Series" and "" (empty owner): 1/64/128/196/255, what Windows Explorer/WMP write;
      they read 1-31, 32-95, 96-159, 160-223, 224-255 as 1-5 stars. rekordbox reads MP3/AIFF rating tags
      (Explorer-set ones show up) only when a track is first imported; Reload Tag never re-reads them, and
      its stars live in its own DB.
    - "Serato": same bytes. Serato DJ 4.0 reads/writes 1/64/128/196/255 in POPM (Re-Scan File Info picks up
      outside edits). Its owner string is undocumented ("Serato" is inferred from one forum post), so the
      other frames also cover a reader that takes any POPM. Serato 3 and older ignore POPM.
    - "traktor@native-instruments.de": 51/102/153/204/255, Traktor's own format and the only owner it reads
      (outside edits sync after "Check Consistency"). Read with Windows ranges these would show 1-2 stars as 2-3.
    No POPM support: Apple Music/Finder (ratings live in the library DB), VirtualDJ, Engine DJ, and Mixxx 2.x
    (a tag-rating import PR is still unmerged).
    """
    stars = _energy_to_stars(rating)
    with _editing(path) as tags:
        for email, steps in _POPM_PROFILES:
            key = f"POPM:{email}"
            if key in tags:
                del tags[key]
            tags.add(id3.POPM(email=email, rating=steps[stars - 1], count=0))


def set_track_number(path: Path, n: int, total: int) -> None:
    """Replace only the track number (TRCK "n/total"); all other tags are left as they are."""
    n = max(1, int(n))
    value = f"{n}/{max(n, int(total))}"
    with _editing(path) as tags:
        tags.setall("TRCK", [id3.TRCK(encoding=0, text=[value])])


def _text_of(tags: id3.ID3, key: str) -> str | None:
    frame = tags.get(key)
    if frame is None or not getattr(frame, "text", None):
        return None
    return str(frame.text[0]).strip() or None


def _popm_stars(tags: id3.ID3) -> int | None:
    """Stars (1-5) from the first rated POPM frame: our own owners first, then any other by Windows' ranges."""
    frames = [(tags.get(f"POPM:{email}"), steps) for email, steps in _POPM_PROFILES]
    known = {f"POPM:{email}" for email, _ in _POPM_PROFILES}
    frames += [(f, _POPM_STD) for key, f in tags.items() if key.startswith("POPM:") and key not in known]
    for frame, steps in frames:
        if frame is None or frame.rating <= 0:
            continue
        if steps is _POPM_TRAKTOR:
            return min(5, max(1, round(frame.rating / 51)))
        return next(stars for stars, top in enumerate(_WINDOWS_READ_RANGES, 1) if frame.rating <= top)
    return None


def read_tags_summary(path: Path) -> dict:
    """Small read-only digest of a finished MP3's tags; None for anything absent. Never raises."""
    summary: dict = dict.fromkeys(
        ("genre", "energy_stars", "track_number", "label", "isrc", "mix_name", "replaygain_db", "spotify_id")
    )
    try:
        tags = id3.ID3(path)
        summary["genre"] = _text_of(tags, "TCON")
        summary["label"] = _text_of(tags, "TPUB")
        summary["isrc"] = _text_of(tags, "TSRC")
        summary["mix_name"] = _text_of(tags, "TIT3")
        summary["spotify_id"] = _text_of(tags, SPOTIFY_ID_KEY)
        summary["energy_stars"] = _popm_stars(tags)
        if m := re.match(r"\s*(\d+)", _text_of(tags, "TRCK") or ""):
            summary["track_number"] = int(m.group(1))
        if m := re.search(r"[-+]?\d+(?:\.\d+)?", _text_of(tags, "TXXX:REPLAYGAIN_TRACK_GAIN") or ""):
            summary["replaygain_db"] = float(m.group())
    except Exception as e:  # unreadable/corrupt file or odd frame: report what we have
        log.debug("read_tags_summary %s: %s", path, e)
    return summary
