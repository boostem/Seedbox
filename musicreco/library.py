"""On-disk library: the crates folder, folder/file naming, duplicate lookup, tracks.csv, skipped.txt, playlists,
crate metadata (.seedbox/crate.json), set order edits and the one-off move out of the legacy downloads/ folder."""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
import shutil
import tempfile
import threading
import unicodedata
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mutagen.id3 import ID3

from .models import BeatportInfo, DownloadResult, Score, Track, YTCandidate
from .settings import load_settings

log = logging.getLogger(__name__)

SEEDBOX_DIR = ".seedbox"  # per-crate app data (crate.json; structure analyses live here too)
CRATE_JSON = "crate.json"
CRATE_JSON_VERSION = 1

MAX_NAME_BYTES = 180  # leaves headroom under APFS's 255 for " [id6]" + ".mp3"
ID_DESC = "SPOTIFY_TRACK_ID"

CSV_COLUMNS = [
    "role", "ticked", "passed_rule", "rule_reason", "spotify_id", "artists", "title", "album",
    "release_date", "duration", "key", "camelot", "bpm", "energy", "danceability", "popularity",
    "genre", "genre_source", "status", "youtube_url", "file", "reason",
    "score", "move", "energy_rating", "order", "lufs", "replaygain_db", "beatport_url", "label", "catalog_number",
    "isrc", "mix_name", "beatport_key", "beatport_camelot", "beatport_bpm", "key_mismatch", "bpm_mismatch",
]  # fmt: skip
# What happened to the file (vs. what tunebat/the checklist say about the track): carried over from the
# previous tracks.csv when this run didn't produce a new outcome for the track.
OUTCOME_COLUMNS = [
    "genre", "genre_source", "status", "youtube_url", "file", "reason", "energy_rating", "order", "lufs",
    "replaygain_db", "beatport_url", "label", "catalog_number", "isrc", "mix_name", "beatport_key",
    "beatport_camelot", "beatport_bpm", "key_mismatch", "bpm_mismatch",
]  # fmt: skip
BPM_MISMATCH_TOL = 0.02
_CAMELOT_RE = re.compile(r"\s*0?(\d{1,2})\s*([AB])\s*", re.IGNORECASE)


def sanitize_filename(s: str) -> str:
    """Make `s` safe as a single macOS file/folder name while keeping it readable."""
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r":(?=\s)", " -", s)  # "A: B" -> "A - B"
    s = s.replace(":", "-").replace("/", "-").replace("\\", "-")
    s = re.sub(r"\s+", " ", s)  # tabs/newlines/line separators become single spaces
    s = "".join(c for c in s if unicodedata.category(c) != "Cc").strip()
    s = _truncate_bytes(s, MAX_NAME_BYTES)
    s = s.lstrip(". ").rstrip(". ")  # no hidden files, no trailing dots/spaces
    return s or "Unknown"


def _truncate_bytes(s: str, limit: int) -> str:
    """Cut `s` to at most `limit` UTF-8 bytes without splitting a character."""
    return s.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


# (mtime_ns, size) -> id, so repeated library scans don't re-parse every tag (covers are big).
_id_cache: dict[Path, tuple[tuple[int, int], str | None]] = {}
_id_cache_lock = threading.Lock()


def _spotify_id(path: Path) -> str | None:
    """Read TXXX:SPOTIFY_TRACK_ID from an MP3, or None if absent/unreadable."""
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    with _id_cache_lock:
        hit = _id_cache.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    found: str | None = None
    try:
        for frame in ID3(path).getall("TXXX"):
            if frame.desc == ID_DESC and frame.text:
                found = str(frame.text[0]).strip() or None
                break
    except Exception:  # not an MP3 / no tag / corrupt: treat as unknown
        found = None
    with _id_cache_lock:
        _id_cache[path] = (sig, found)
    return found


def downloads_root() -> Path:
    """The crates folder: Settings.output_root (MUSICRECO_OUTPUT_ROOT overrides it), read at call time."""
    return load_settings().output_root


_crate_locks: dict[str, threading.RLock] = {}
_crate_locks_guard = threading.Lock()


def crate_lock(seed_dir: Path) -> threading.RLock:
    """Process-wide re-entrant lock for one crate folder; held while its tags/CSV/playlists are rewritten."""
    key = os.path.realpath(seed_dir)
    with _crate_locks_guard:
        return _crate_locks.setdefault(key, threading.RLock())


@contextmanager
def preserve_mtime(path: Path) -> Iterator[None]:
    """Put `path`'s access/modification times back after a tag-only edit (TRCK, POPM).

    Audio analysis caches are keyed on (size, mtime); renumbering a set must not invalidate them.
    """
    try:
        st = path.stat()
    except OSError:
        yield
        return
    try:
        yield
    finally:
        try:
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            pass


def seed_dir(seed: Track, root: Path | None = None) -> Path:
    """Return (creating it) the folder for a seed: <root>/<Artists - Title>/ (root defaults to downloads_root())."""
    d = (root if root is not None else downloads_root()) / sanitize_filename(seed.display_name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def track_path(seed_dir: Path, track: Track) -> Path:
    """Target .mp3 path in `seed_dir`; adds ' [id6]' if the plain name belongs to another track."""
    stem = sanitize_filename(track.display_name)
    path = seed_dir / f"{stem}.mp3"
    if not path.exists() or _spotify_id(path) == track.id:
        return path
    for n in (6, 10, len(track.id)):  # widen the id suffix in the (unlikely) event of a clash
        path = seed_dir / f"{stem} [{track.id[:n]}].mp3"
        if not path.exists() or _spotify_id(path) == track.id:
            break
    return path


def _iter_mp3s(root: Path) -> Iterator[Path]:
    """Every .mp3 under `root`, skipping hidden folders (the pipeline's .tmp-* work dirs, .Trashes, ...)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.lower().endswith(".mp3") and not name.startswith("."):
                yield Path(dirpath) / name


def _crate_mp3s(seed_dir: Path) -> list[Path]:
    """The crate's own MP3s (top level of the folder, no hidden files), by name."""
    try:
        names = os.listdir(seed_dir)
    except OSError:
        return []
    paths = (seed_dir / n for n in names if n.lower().endswith(".mp3") and not n.startswith("."))
    return sorted((p for p in paths if p.is_file()), key=lambda p: p.name)


def find_existing(track: Track, root: Path | None = None) -> list[Path]:
    """All MP3s under `root` (default downloads_root()) tagged with this track's Spotify ID."""
    root = root if root is not None else downloads_root()
    if not root.is_dir():
        return []
    return sorted(p for p in _iter_mp3s(root) if p.is_file() and _spotify_id(p) == track.id)


class LibraryIndex:
    """Spotify ID -> MP3s under `root`: one scan (on first lookup) per batch instead of one per track.

    Thread-safe. `add` records files written after the scan; lookups re-check that each file is still
    there and still carries the ID.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else downloads_root()
        self._by_id: dict[str, set[Path]] | None = None
        self._lock = threading.Lock()

    def _scan(self) -> dict[str, set[Path]]:
        by_id: dict[str, set[Path]] = {}
        if self.root.is_dir():
            for p in _iter_mp3s(self.root):
                if tid := _spotify_id(p):
                    by_id.setdefault(tid, set()).add(p)
        return by_id

    def find(self, track_id: str) -> list[Path]:
        """Like find_existing, from the index."""
        with self._lock:
            if self._by_id is None:
                self._by_id = self._scan()
            paths = list(self._by_id.get(track_id, ()))
        return sorted(p for p in paths if p.is_file() and _spotify_id(p) == track_id)

    def add(self, path: Path, track_id: str) -> None:
        with self._lock:
            if self._by_id is not None:  # otherwise the first scan will pick it up
                self._by_id.setdefault(track_id, set()).add(path)


@dataclass
class CsvRow:
    """One line of tracks.csv: a track plus its checklist state and download outcome."""

    track: Track
    role: str  # "seed" | "r"
    passed_rule: bool | None = None
    rule_reason: str = ""
    ticked: bool = True
    result: DownloadResult | None = None
    score: Score | None = None


def _yn(v: bool | None) -> str:
    return "" if v is None else ("yes" if v else "no")


def _pct(x: float) -> int:
    return round(x * 100)


def _cell(v: object) -> str:
    return "" if v is None else str(v)


def _finite(x: object) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def _num(x: float | None, fmt: str) -> str:
    return format(x, fmt) if _finite(x) else ""


def _camelot(code: object) -> str | None:
    """'01a ' -> '1A'; None if not a Camelot code."""
    m = _CAMELOT_RE.fullmatch(code) if isinstance(code, str) else None
    return f"{int(m.group(1))}{m.group(2).upper()}" if m and 1 <= int(m.group(1)) <= 12 else None


def key_mismatch(tunebat_camelot: str | None, beatport_camelot: str | None) -> bool | None:
    """True if Beatport's key differs from tunebat's (None when either is unknown)."""
    a, b = _camelot(tunebat_camelot), _camelot(beatport_camelot)
    return None if a is None or b is None else a != b


def bpm_mismatch(tunebat_bpm: float | None, beatport_bpm: float | None, tol: float = BPM_MISMATCH_TOL) -> bool | None:
    """True if the BPMs differ by more than `tol` even after half/double-time (None when either is unknown)."""
    if not (_finite(tunebat_bpm) and _finite(beatport_bpm) and tunebat_bpm > 0 and beatport_bpm > 0):
        return None
    return min(abs(beatport_bpm * f - tunebat_bpm) for f in (1, 2, 0.5)) > tol * tunebat_bpm


def _csv_values(row: CsvRow) -> dict[str, str]:
    t, r, s = row.track, row.result, row.score
    bp = (r.beatport if r else None) or BeatportInfo()
    loud = r.loudness if r else None
    values = [
        row.role,
        _yn(row.ticked),
        _yn(row.passed_rule),
        row.rule_reason,
        t.id,
        t.artist_str,
        t.name,
        t.album,
        t.release_date,
        t.duration_str,
        t.key,
        t.camelot,
        round(t.bpm) if t.bpm else "",
        _pct(t.energy),
        _pct(t.danceability),
        t.popularity,
        r.genre if r else None,
        r.genre_source if r else None,
        r.status if r else None,
        r.youtube.url if r and r.youtube else None,
        r.path.name if r and r.path else None,
        r.reason if r else None,
        s.value if s else None,
        s.move if s else None,
        r.energy_rating if r else None,
        r.order if r else None,
        _num(loud.lufs, ".1f") if loud else None,
        _num(loud.replaygain_db, ".2f") if loud else None,
        bp.url,
        bp.label,
        bp.catalog_number,
        bp.isrc,
        bp.mix_name,
        bp.key,
        bp.camelot,
        _num(bp.bpm, "g"),
        _yn(key_mismatch(t.camelot, bp.camelot)),
        _yn(bpm_mismatch(t.bpm, bp.bpm)),
    ]
    return {col: _cell(v) for col, v in zip(CSV_COLUMNS, values, strict=True)}


_CSV_ERRORS = (OSError, UnicodeError, csv.Error, ValueError)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """(header, rows) of a tracks.csv; raises one of _CSV_ERRORS if it can't be read or has no spotify_id column."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = [{k: v or "" for k, v in r.items() if k is not None} for r in reader]
        header = list(reader.fieldnames or [])
    if "spotify_id" not in header:
        raise ValueError("no spotify_id column")
    return header, rows


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    """Atomic tracks.csv write (UTF-8 with BOM, temp file + fsync + rename), keeping the old file's permissions."""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tracks.csv.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _previous_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """(header, rows) of an existing tracks.csv; empty if there is none or it can't be read (then backed up)."""
    if not path.exists():
        return [], []
    try:
        return _read_csv(path)
    except _CSV_ERRORS as e:
        backup = path.with_name(path.name + ".bak")
        log.warning("can't merge with %s (%s); writing a fresh one, old copy kept as %s", path, e, backup.name)
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass
        return [], []


def _merge_previous(new: dict[str, str], prev: dict[str, str], result: DownloadResult | None, seed_dir: Path) -> None:
    """Fold the previous run's row for the same track into this run's row (see write_tracks_csv)."""
    for col, value in prev.items():
        if col not in CSV_COLUMNS:  # a column the user added in Excel/Numbers
            new[col] = value
    prev_file = prev.get("file", "")
    file_still_here = bool(prev_file) and (seed_dir / prev_file).is_file()
    if result is None or (result.status in ("failed", "skipped") and file_still_here):
        for col in OUTCOME_COLUMNS:
            new[col] = prev.get(col, "")
    elif result.status == "exists":
        for col in OUTCOME_COLUMNS:
            new[col] = new[col] or prev.get(col, "")


def write_tracks_csv(seed_dir: Path, seed: Track, rows: list[CsvRow]) -> Path:
    """Write <seed_dir>/tracks.csv atomically (UTF-8 with BOM so Excel/Numbers read it), merged with the old one.

    This run's rows come first and win, by Spotify ID, except: a track with no new outcome (not ticked,
    or failed/skipped while its earlier file is still in the folder) keeps its previous outcome columns,
    and an `exists` row fills its blanks (YouTube URL, LUFS, ...) from the previous row. Rows for tracks
    not in this run are kept, after this run's rows. Columns the user added are kept at the end.
    """
    path = seed_dir / "tracks.csv"
    header, old = _previous_rows(path)
    prev_by_id = {r["spotify_id"]: r for r in old if r.get("spotify_id")}
    out: list[dict[str, str]] = []
    done: set[str] = set()
    for row in rows:
        if row.track.id in done:
            continue
        new = _csv_values(row)
        if prev := prev_by_id.get(row.track.id):
            _merge_previous(new, prev, row.result, seed_dir)
        out.append(new)
        done.add(row.track.id)
    out.extend(r for r in old if r.get("spotify_id") not in done)

    _write_csv(path, CSV_COLUMNS + [c for c in header if c not in CSV_COLUMNS], out)
    return path


def _mmss(seconds: float | None) -> str:
    if seconds is None:
        return "?:??"
    m, s = divmod(round(seconds), 60)
    return f"{m}:{s:02d}"


def _candidate_line(c: YTCandidate) -> str:
    return f'"{c.title}" by {c.channel} | {_mmss(c.duration_s)} | {c.url} [{c.source}]'


def _skipped_entry(res: DownloadResult) -> str:
    t = res.track
    bpm = f"{t.bpm:g} BPM" if t.bpm else "? BPM"
    lines = [
        f"{t.artist_str} - {t.name}",
        f"    {t.camelot or '?'} | {bpm} | {t.duration_str} on Spotify",
        f"    Spotify: {t.spotify_url}",
        f"    Status: {res.status}" + (f" - {res.reason}" if res.reason else ""),
    ]
    if res.closest:
        lines.append(f"    Closest YouTube: {_candidate_line(res.closest)}")
    return "\n".join(lines)


def append_skipped(seed_dir: Path, seed: Track, results: list[DownloadResult]) -> Path | None:
    """Append a timestamped block of skipped/failed tracks to skipped.txt; None if nothing to log."""
    bad = [r for r in results if r.status in ("skipped", "failed")]
    if not bad:
        return None
    path = seed_dir / "skipped.txt"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    head = f"=== {stamp} | seed: {seed.display_name} ==="
    block = head + "\n\n" + "\n\n".join(_skipped_entry(r) for r in bad) + "\n"
    needs_gap = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8") as f:
        f.write(("\n" if needs_gap else "") + block)
    return path


_CAMELOT_TO_KEY = {  # Camelot -> musical key with sharps, the notation AutoPilot stores ("C#m", "G")
    "1A": "G#m", "2A": "D#m", "3A": "A#m", "4A": "Fm", "5A": "Cm", "6A": "Gm",
    "7A": "Dm", "8A": "Am", "9A": "Em", "10A": "Bm", "11A": "F#m", "12A": "C#m",
    "1B": "B", "2B": "F#", "3B": "C#", "4B": "G#", "5B": "D#", "6B": "A#",
    "7B": "F", "8B": "C", "9B": "G", "10B": "D", "11B": "A", "12B": "E",
}  # fmt: skip


@dataclass
class CrateFile:
    """One MP3 of a crate as its tags describe it (what the playlists and the set builder need)."""

    path: Path
    track_id: str | None = None  # TXXX:SPOTIFY_TRACK_ID
    number: int | None = None  # track number from TRCK
    trck: str = ""  # raw TRCK text, e.g. "3/12"
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    label: str = ""
    remixer: str = ""
    year: str = ""
    camelot: str = ""
    bpm: float = 0.0
    stars: int = 0
    seconds: int = -1
    bitrate: int = 0
    sample_rate: int = 0
    order: int = 0  # 1-based position in the set (filled by crate_files)
    energy_rating: int | None = None  # 1-10: tracks.csv's value, else from the star rating (filled by crate_files)

    @property
    def name(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist and self.title else self.path.stem


def _crate_file(path: Path) -> CrateFile:
    """Read one MP3's tags; an unreadable file still comes back (sorted last, named after the file)."""
    from mutagen.mp3 import MP3

    f = CrateFile(path)
    try:
        audio = MP3(path)
    except Exception as e:  # noqa: BLE001
        log.warning("couldn't read %s for the playlist: %s", path.name, e)
        return f
    tags = audio.tags or {}

    def text(frame: str) -> str:
        return str(tags[frame].text[0]).strip() if frame in tags and tags[frame].text else ""

    f.title, f.artist, f.album, f.genre = text("TIT2"), text("TPE1"), text("TALB"), text("TCON")
    f.label, f.remixer, f.camelot = text("TPUB"), text("TPE4"), text("TKEY").upper()
    f.year = text("TYER") or text("TDRC")[:4]
    f.track_id = text(f"TXXX:{ID_DESC}") or None
    f.trck = text("TRCK")
    if m := re.match(r"\s*(\d+)", f.trck):
        f.number = int(m.group(1))
    try:
        f.bpm = float(text("TBPM") or 0)
    except ValueError:
        pass
    from .tagging import read_tags_summary  # owns the POPM mapping (several frames, different scales)

    f.stars = read_tags_summary(path).get("energy_stars") or 0
    f.seconds = round(audio.info.length)
    f.bitrate = round((audio.info.bitrate or 0) / 1000)
    f.sample_rate = audio.info.sample_rate or 0
    return f


def _crate_in_order(seed_dir: Path) -> list[CrateFile]:
    """The folder's MP3s in set order (track numbers); unnumbered files last, by name."""
    files = [_crate_file(p) for p in _crate_mp3s(seed_dir)]
    return sorted(files, key=lambda f: (f.number is None, f.number or 0, f.path.name.casefold()))


def crate_files(seed_dir: Path) -> list[CrateFile]:
    """The crate's MP3s in set order with `order` = 1..N; also the energy rating (tracks.csv, else 2 x stars).

    Files without a Spotify ID tag (dropped in by hand) are included with track_id None.
    """
    files = _crate_in_order(seed_dir)
    ratings: dict[str, int] = {}
    if (seed_dir / "tracks.csv").is_file():
        try:
            for row in _read_csv(seed_dir / "tracks.csv")[1]:
                if (v := row.get("energy_rating", "").strip()).isdigit() and 1 <= int(v) <= 10:
                    ratings[row["spotify_id"]] = int(v)
        except _CSV_ERRORS as e:
            log.debug("crate_files %s: tracks.csv unreadable (%s)", seed_dir.name, e)
    for n, f in enumerate(files, 1):
        f.order = n
        f.energy_rating = ratings.get(f.track_id or "") or (min(10, f.stars * 2) if f.stars else None)
    return files


def _write_atomic(dest: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".playlist-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.chmod(tmp, 0o644)  # mkstemp makes 0600 files; playlists are ordinary documents
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_playlist(seed_dir: Path) -> Path | None:
    """Write `<folder name>.m3u8` listing the folder's MP3s in set order (their track numbers); None if empty.

    Built from the files' TRCK tags, so it always matches what finalize_crate wrote and also works for crates
    downloaded before playlists existed. Paths are relative, so the folder can be moved as a whole.
    """
    files = _crate_in_order(seed_dir)
    if not files:
        return None
    lines = ["#EXTM3U", f"#PLAYLIST:{seed_dir.name}"]
    for f in files:
        lines += [f"#EXTINF:{f.seconds},{f.name}", f.path.name]
    dest = seed_dir / f"{sanitize_filename(seed_dir.name)}.m3u8"
    _write_atomic(dest, "\n".join(lines) + "\n")
    return dest


def write_rekordbox_xml(seed_dir: Path) -> Path | None:
    """Write `<folder name>.xml`: the crate as a playlist in set order, in rekordbox's XML exchange format.

    For DJ apps that import rekordbox XML but ignore track numbers and M3U (AutoPilot 0.1.x: its folder
    import orders tracks by analysis completion; its XML import keeps playlist order). Carries title/artist/
    album/genre/label/remixer/year, BPM, key (musical, sharps), energy stars as Rating, and absolute file URLs.
    None if the folder has no MP3s.
    """
    from urllib.parse import quote
    from xml.etree import ElementTree as ET

    files = _crate_in_order(seed_dir)
    if not files:
        return None
    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(root, "PRODUCT", Name="MusicReco", Version="1", Company="")
    collection = ET.SubElement(root, "COLLECTION", Entries=str(len(files)))
    for n, f in enumerate(files, 1):
        attrs = {
            "TrackID": str(n),
            "Name": f.title or f.path.stem,
            "Artist": f.artist,
            "Album": f.album,
            "Genre": f.genre,
            "Label": f.label,
            "Remixer": f.remixer,
            "Kind": "MP3 File",
            "Size": str(f.path.stat().st_size),
            "TotalTime": str(max(f.seconds, 0)),
            "Year": f.year,
            "AverageBpm": f"{f.bpm:.2f}" if f.bpm else "",
            "BitRate": str(f.bitrate) if f.bitrate else "",
            "SampleRate": str(f.sample_rate) if f.sample_rate else "",
            "TrackNumber": str(f.number or ""),
            "Tonality": _CAMELOT_TO_KEY.get(f.camelot, ""),
            "Rating": str(f.stars * 51),  # rekordbox XML scale: 0, 51, 102, 153, 204, 255
            "Location": "file://localhost" + quote(str(f.path.resolve()), safe="/"),
        }
        ET.SubElement(collection, "TRACK", {k: v for k, v in attrs.items() if v != ""})
    playlists = ET.SubElement(root, "PLAYLISTS")
    top = ET.SubElement(playlists, "NODE", Type="0", Name="ROOT", Count="1")
    node = ET.SubElement(top, "NODE", Name=seed_dir.name, Type="1", KeyType="0", Entries=str(len(files)))
    for n in range(1, len(files) + 1):
        ET.SubElement(node, "TRACK", Key=str(n))
    ET.indent(root)
    dest = seed_dir / f"{sanitize_filename(seed_dir.name)}.xml"
    _write_atomic(dest, '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n")
    return dest


# --- crate metadata: .seedbox/crate.json -------------------------------------------------------------------------


def crate_json_path(seed_dir: Path) -> Path:
    return seed_dir / SEEDBOX_DIR / CRATE_JSON


def _now_iso(ts: float | None = None) -> str:
    when = datetime.fromtimestamp(ts, timezone.utc) if ts is not None else datetime.now(timezone.utc)
    return when.isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_crate_json(seed_dir: Path) -> dict | None:
    path = crate_json_path(seed_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable %s: %s", path, e)
        return None
    return data if isinstance(data, dict) else None


def write_crate_json(seed_dir: Path, seed: Track, recs: list[Track]) -> Path:
    """Write `seed_dir/.seedbox/crate.json`: tunebat's raw dicts for the seed and its recommendations.

    Keys: version, seed, recs (this run's list, in order), earlier (tracks from previous runs that are no longer
    recommended, so their files stay describable), updated_at. Atomic; merged with the previous file.
    """
    previous = _read_crate_json(seed_dir) or {}
    recs_raw: dict[str, dict] = {}
    for t in recs:
        if t.id != seed.id:
            recs_raw.setdefault(t.id, t.to_raw())
    earlier: dict[str, dict] = {}
    for raw in [previous.get("seed"), *(previous.get("recs") or []), *(previous.get("earlier") or [])]:
        tid = raw.get("id") if isinstance(raw, dict) else None
        if isinstance(tid, str) and tid and tid != seed.id and tid not in recs_raw:
            earlier.setdefault(tid, raw)
    data = {
        "version": CRATE_JSON_VERSION,
        "seed": seed.to_raw(),
        "recs": list(recs_raw.values()),
        "earlier": list(earlier.values()),
        "updated_at": _now_iso(),
    }
    path = crate_json_path(seed_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    return path


def _track_from_raw(raw: object) -> Track | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    try:
        return Track.from_api(raw)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _num_cell(text: str, kind: type = float) -> Any:
    try:
        v = float(text)
    except (TypeError, ValueError):
        return 0
    return kind(v) if math.isfinite(v) else 0


def _clock_ms(text: str) -> int:
    parts = text.strip().split(":")
    if not all(p.isdigit() for p in parts) or not 1 <= len(parts) <= 3:
        return 0
    secs = 0
    for p in parts:
        secs = secs * 60 + int(p)
    return secs * 1000


def _track_from_csv(row: dict[str, str]) -> Track | None:
    """Rebuild a Track from a tracks.csv row (lossy: BPM rounded, energy to 2 places, no cover)."""
    tid = row.get("spotify_id", "").strip()
    if not tid:
        return None
    return Track.from_api({
        "id": tid, "n": row.get("title", ""), "as": [a for a in row.get("artists", "").split(", ") if a],
        "an": row.get("album", ""), "rd": row.get("release_date", ""), "d": _clock_ms(row.get("duration", "")),
        "p": _num_cell(row.get("popularity", ""), int), "k": row.get("key", ""), "c": row.get("camelot", ""),
        "b": _num_cell(row.get("bpm", "")), "e": _num_cell(row.get("energy", "")) / 100,
        "da": _num_cell(row.get("danceability", "")) / 100, "_source": "tracks.csv",
    })  # fmt: skip


def _track_from_tags(path: Path) -> Track | None:
    """Rebuild a Track from an MP3's own tags (no energy/danceability/popularity); None without a Spotify ID."""
    f = _crate_file(path)
    if not f.track_id:
        return None
    title = f.title or path.stem
    return Track.from_api({
        "id": f.track_id, "n": title, "as": [a for a in f.artist.split(", ") if a], "an": f.album, "rd": f.year,
        "d": max(f.seconds, 0) * 1000, "c": f.camelot, "b": f.bpm, "_source": "id3",
    })  # fmt: skip


def _load_crate(seed_dir: Path, scan_files: bool = True) -> tuple[Track | None, dict[str, Track]]:
    seed: Track | None = None
    tracks: dict[str, Track] = {}
    data = _read_crate_json(seed_dir)
    if data:
        seed = _track_from_raw(data.get("seed"))
        for raw in [data.get("seed"), *(data.get("recs") or []), *(data.get("earlier") or [])]:
            if (t := _track_from_raw(raw)) is not None:
                tracks.setdefault(t.id, t)
    csv_path = seed_dir / "tracks.csv"
    if csv_path.is_file():
        try:
            rows = _read_csv(csv_path)[1]
        except _CSV_ERRORS as e:
            log.debug("load_crate %s: tracks.csv unreadable (%s)", seed_dir.name, e)
            rows = []
        for row in rows:
            if (t := _track_from_csv(row)) is not None:
                t = tracks.setdefault(t.id, t)
                if seed is None and row.get("role") == "seed":
                    seed = t
    if seed is None:  # the seed's own file is named like the folder
        own = seed_dir / f"{seed_dir.name}.mp3"
        tid = _spotify_id(own) if own.is_file() else None
        seed = tracks.get(tid) if tid else None
        if seed is None and tid and (seed := _track_from_tags(own)) is not None:
            tracks[seed.id] = seed
    if scan_files:
        for path in _crate_mp3s(seed_dir):
            tid = _spotify_id(path)
            if tid and tid not in tracks and (t := _track_from_tags(path)) is not None:
                tracks[t.id] = t
    if seed is not None:
        tracks = {seed.id: seed, **{k: v for k, v in tracks.items() if k != seed.id}}
    return seed, tracks


def load_crate_tracks(seed_dir: Path) -> dict[str, Track]:
    """Every track the crate knows, by Spotify ID, seed first.

    Sources, best first: crate.json (exact tunebat data), tracks.csv rows (old crates; BPM rounded, no cover),
    then the ID3 tags of any MP3 in the folder neither of them covers. Rebuilt tracks carry raw["_source"].
    """
    return _load_crate(seed_dir)[1]


def load_crate_seed(seed_dir: Path) -> Track | None:
    """The crate's seed track (same sources as load_crate_tracks), or None if it can't be told."""
    return _load_crate(seed_dir, scan_files=False)[0]


# --- set order edits, crate listing --------------------------------------------------------------------------------


def apply_order(seed_dir: Path, ordered_ids: Iterable[str]) -> None:
    """Renumber the crate in the given order: TRCK "n/total" on every MP3, then `.m3u8`, `.xml` and the
    tracks.csv `order` column. IDs without a file are ignored; files not listed keep their relative order after
    the listed ones. Tag edits keep each file's mtime (see preserve_mtime); unchanged files aren't rewritten.
    A file that can't be tagged is logged and skipped. Holds crate_lock(seed_dir).
    """
    with crate_lock(seed_dir):
        files = _crate_in_order(seed_dir)
        by_id: dict[str, CrateFile] = {}
        for f in files:
            if f.track_id:
                by_id.setdefault(f.track_id, f)
        first = [by_id[i] for i in dict.fromkeys(ordered_ids) if i in by_id]
        chosen = {id(f) for f in first}
        final = first + [f for f in files if id(f) not in chosen]
        total = len(final)
        orders: dict[str, int] = {}
        from .tagging import set_track_number

        for n, f in enumerate(final, 1):
            if f.track_id:
                orders.setdefault(f.track_id, n)
            if f.trck == f"{n}/{total}":
                continue
            try:
                with preserve_mtime(f.path):
                    set_track_number(f.path, n, total)
            except Exception as e:  # noqa: BLE001 - one locked/vanished file must not stop the rest
                log.warning("couldn't renumber %s: %s", f.path.name, e)
        write_playlist(seed_dir)
        write_rekordbox_xml(seed_dir)
        _set_csv_orders(seed_dir, orders)


def _set_csv_orders(seed_dir: Path, orders: dict[str, int]) -> None:
    """Rewrite tracks.csv's `order` column (blank for tracks without a file); no-op without a readable CSV."""
    path = seed_dir / "tracks.csv"
    if not path.is_file():
        return
    try:
        header, rows = _read_csv(path)
    except _CSV_ERRORS as e:
        log.warning("not updating the order column of %s: %s", path, e)
        return
    for row in rows:
        n = orders.get(row.get("spotify_id", ""))
        row["order"] = str(n) if n else ""
    _write_csv(path, CSV_COLUMNS + [c for c in header if c not in CSV_COLUMNS], rows)


def list_crates(root: Path | None = None) -> list[dict]:
    """CrateSummary dicts (types.ts) for every crate folder under `root` (default downloads_root()), newest first.

    A crate folder is a non-hidden folder holding MP3s, a tracks.csv or a .seedbox/crate.json. updated_at is the
    newest mtime of the folder, its MP3s and its metadata files (ISO 8601, UTC).
    """
    root = root if root is not None else downloads_root()
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return []
    out: list[dict] = []
    for d in entries:
        if d.name.startswith(".") or not d.is_dir():
            continue
        mp3s = _crate_mp3s(d)
        meta = [p for p in (d / "tracks.csv", crate_json_path(d)) if p.is_file()]
        if not mp3s and not meta:
            continue
        stamps = []
        for p in [d, *mp3s, *meta]:
            try:
                stamps.append(p.stat().st_mtime)
            except OSError:
                pass
        seed = load_crate_seed(d)
        out.append({
            "folder": d.name, "path": str(d), "seed": seed.to_json() if seed else None,
            "track_count": len(mp3s), "updated_at": _now_iso(max(stamps)) if stamps else _now_iso(),
        })  # fmt: skip
    out.sort(key=lambda c: c["updated_at"], reverse=True)
    return out
