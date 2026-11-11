"""The library view (#73): one compact row per track for the library table, and the overview waveforms it draws.

A row says what the table shows and filters on: BPM, key (as a Camelot code), energy (0-1), genre, whether there is
artwork, and the track's analysis status. Our own analyses win (`beats`/`tempo` → `bpm`, `key` → `camelot` or `key`,
`energy` → `energy`); until a track has them, the hints its tags carry fill in (the file's tags, Rekordbox, iTunes,
Seedbox 1/tunebat), marked `bpm_from`/`key_from`/`energy_from` = "tags" so the UI can show them as hints.

The key (#45): the user's override wins (`key_from` "user"), then our analysis ("analysis"). An analysis that isn't
confident is `key` None with `key_from` "unknown", shown as "?". Once either of those speaks, the tags stop filling
in: the first hint that disagrees is `key_hint`, from `key_hint_from`, to show next to ours.

Status (`state`): `analysing` while one of its jobs runs, `queued` while one waits, `failed` if the latest run of a
job failed, `done` once it has a waveform and nothing is pending, else `new`. "Not analysed yet" is every state but
`done`.

Overviews: `pack_overviews` puts the 1,200-column peak overview of each band (low, mid, high) of many tracks in one
binary response, so the table fetches what is on screen in one request:

    header, 12 bytes: magic b"SBOV", columns u16, bands u8, 1 byte padding, tracks u32 (little-endian)
    per track, in the order asked: present u8 (0 or 1), then if present bands × columns u8 (peak bytes, as in the
    waveform file: amplitude = 10 ** ((byte / 255 - 1) * floor_dB / 20))
"""

from __future__ import annotations

import gzip
import re
import struct
import threading
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

OVERVIEW_MAGIC = b"SBOV"
OVERVIEW_HEADER = "<4sHBxI"

# --- keys ----------------------------------------------------------------------------------------------------------

_NOTES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
# Camelot number of each pitch class as a major key (B) and as a minor key (A)
_MAJOR = {0: 8, 7: 9, 2: 10, 9: 11, 4: 12, 11: 1, 6: 2, 1: 3, 8: 4, 3: 5, 10: 6, 5: 7}
_MINOR = {9: 8, 4: 9, 11: 10, 6: 11, 1: 12, 8: 1, 3: 2, 10: 3, 5: 4, 0: 5, 7: 6, 2: 7}
_CAMELOT = re.compile(r"^\s*0?(\d{1,2})\s*([AB])\s*$", re.I)
_OPEN_KEY = re.compile(r"^\s*(\d{1,2})\s*([DM])\s*$", re.I)  # Traktor's Open Key: 1d = 8B, 1m = 8A
_NAMED = re.compile(r"^\s*([A-G])\s*(#|♯|b|♭|-?sharp|-?flat)?\s*(.*?)\s*$", re.I)


def to_camelot(key: Any) -> str | None:
    """A key in any common notation ("8A", "Am", "A minor", "F#m", "Dbmaj", "1m") as a Camelot code, or None."""
    if not isinstance(key, str) or not key.strip():
        return None
    if m := _CAMELOT.match(key):
        n = int(m[1])
        return f"{n}{m[2].upper()}" if 1 <= n <= 12 else None
    if m := _OPEN_KEY.match(key):
        n = int(m[1])
        return f"{(n + 6) % 12 + 1}{'B' if m[2].lower() == 'd' else 'A'}" if 1 <= n <= 12 else None
    m = _NAMED.match(key)
    if not m:
        return None
    pc = _NOTES[m[1].upper()]
    acc = (m[2] or "").lower()
    pc = (pc + (1 if acc in ("#", "♯", "sharp", "-sharp") else -1 if acc in ("b", "♭", "flat", "-flat") else 0)) % 12
    mode = m[3].lower().replace(".", "")
    if mode in ("m", "min", "minor", "mi", "moll"):
        return f"{_MINOR[pc]}A"
    if mode in ("", "maj", "major", "ma", "dur"):
        return f"{_MAJOR[pc]}B"
    return None


# --- rows ----------------------------------------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f > 0 else None  # not NaN, positive


def _hint_sources(tags: dict) -> list[dict]:
    """Where tag hints live, most trusted first: Rekordbox's analysis, Seedbox 1 (tunebat), the file, iTunes."""
    out = [tags.get(k) for k in ("rekordbox",)]
    out.append(tags)  # Seedbox 1's import keeps camelot/bpm/genre/energy_rating at the top level
    out += [tags.get(k) for k in ("tunebat", "file", "itunes")]
    return [d for d in out if isinstance(d, dict)]


def _hint_bpm(tags: dict) -> float | None:
    for d in _hint_sources(tags):
        if (bpm := _num(d.get("bpm") or d.get("tempo") or d.get("b"))) and 40 <= bpm <= 300:
            return round(bpm, 2)
    return None


def key_hints(tags: dict) -> list[dict]:
    """The keys the tags claim, most trusted first, one per key: `[{"key": "8A", "from": "rekordbox"}]`."""
    out: dict[str, str] = {}
    for name, d in (("rekordbox", tags.get("rekordbox")), ("tunebat", tags), ("tunebat", tags.get("tunebat")),
                    ("file", tags.get("file")), ("itunes", tags.get("itunes"))):  # fmt: skip
        if isinstance(d, dict):
            for k in ("camelot", "key", "c", "k"):
                if code := to_camelot(d.get(k)):
                    out.setdefault(code, name)
    return [{"key": code, "from": name} for code, name in out.items()]


def _hint_key(tags: dict) -> str | None:
    hints = key_hints(tags)
    return hints[0]["key"] if hints else None


def _energy01(v: Any) -> float | None:
    """0-1 energy from a 0-1 float or a 1-10 rating."""
    e = _num(v)
    if e is None:
        return None
    return round(min(e / 10 if e > 1 else e, 1.0), 3)


def _hint_energy(tags: dict) -> float | None:
    tb = tags.get("tunebat") if isinstance(tags.get("tunebat"), dict) else {}
    for v in (tags.get("energy_rating"), tb.get("energy"), tb.get("e")):
        if (e := _energy01(v)) is not None:
            return e
    return None


def hint_genre(tags: dict) -> str:
    """The genre a track's tags give (its file's, or Seedbox 1's import's), "" without one."""
    for d in _hint_sources(tags):
        g = d.get("genre")
        if isinstance(g, str) and g.strip():
            return g.strip()
        if isinstance(g, list) and g and isinstance(g[0], str):
            return g[0]
    return ""


def _text(tags: dict) -> str:
    """The free text a "vibe" search looks through besides title, artist and album: genre, comment, label, remixer."""
    parts = []
    for d in _hint_sources(tags):
        for k in ("genre", "comment", "label", "remixer", "mood", "grouping"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
    return " · ".join(dict.fromkeys(parts))


_STEMS_PENDING = ("queued", "running", "failed")  # a job that ended otherwise left stems, or skipped them, or they were evicted


def describe(track: dict, *, analyses: dict[str, dict] | None = None, artifacts: Iterable[str] = (),
             jobs: dict[str, dict] | None = None, waveform_version: str | None = None) -> dict:  # fmt: skip
    """The table row for a track. `analyses`: analyser → its newest data's fields (bpm, camelot, key, energy);
    `artifacts`: "kind:model_version" strings; `jobs`: kind → the newest job about it ({state, progress})."""
    tags = track.get("tags") or {}
    analyses = analyses or {}
    jobs = jobs or {}
    arts = set(artifacts)
    row: dict[str, Any] = {k: track[k] for k in ("hash", "duration", "title", "artist", "album", "source", "added_at")}

    bpm = next((b for a in ("beats", "tempo") if (b := _num(analyses.get(a, {}).get("bpm")))), None)
    row["bpm"], row["bpm_from"] = (round(bpm, 2), "analysis") if bpm else (_hint_bpm(tags), "tags")
    a_key = analyses.get("key", {})
    override = to_camelot(analyses.get("key_override", {}).get("camelot"))
    key = to_camelot(a_key.get("camelot")) or to_camelot(a_key.get("key"))
    if override:
        row["key"], row["key_from"] = override, "user"
    elif key:
        row["key"], row["key_from"] = key, "analysis"
    elif "key" in analyses:
        row["key"], row["key_from"] = None, "unknown"
    else:
        row["key"], row["key_from"] = _hint_key(tags), "tags"
    hint = next((h for h in key_hints(tags) if h["key"] != row["key"]), None) if row["key_from"] != "tags" else None
    row["key_hint"], row["key_hint_from"] = (hint["key"], hint["from"]) if hint else (None, None)
    measured = analyses.get("energy", {}).get("energy")
    energy = 0.0 if measured == 0 else _energy01(measured)  # 0 is a real answer here: a track rated 1 of 10
    row["energy"], row["energy_from"] = (energy, "analysis") if energy is not None else (_hint_energy(tags), "tags")
    for k in ("bpm", "energy"):
        if row[k] is None:
            row[f"{k}_from"] = None
    if row["key"] is None and row["key_from"] == "tags":
        row["key_from"] = None
    row["genre"] = hint_genre(tags)
    row["text"] = _text(tags)
    row["artwork"] = bool(tags.get("artwork") or (tags.get("tunebat") or {}).get("cover_url"))
    row["format"] = str((tags.get("file") or {}).get("format") or "")

    waveform = any(a.startswith("waveform:") and (waveform_version is None or a == f"waveform:{waveform_version}")
                   for a in arts)  # fmt: skip
    stems = any(a.startswith("stems:") for a in arts)
    states = {j["state"] for j in jobs.values()}
    if "running" in states:
        state = "analysing"
    elif "queued" in states:
        state = "queued"
    elif "failed" in states:
        state = "failed"
    else:
        state = "done" if waveform else "new"
    stems_job = jobs.get("stems")
    row["status"] = {
        "state": state,
        "waveform": waveform,
        "stems": True if stems else (stems_job["state"] if stems_job and stems_job["state"] in _STEMS_PENDING else False),
        "stems_progress": stems_job.get("progress") if stems_job and stems_job["state"] == "running" else None,
        "analyses": sorted(a for a in analyses if a != "key_override"),
        "jobs": {k: j["state"] for k, j in sorted(jobs.items())},
    }
    return row


# --- overviews ------------------------------------------------------------------------------------------------------


class OverviewCache:
    """The peak overview rows of waveform files, read once per file version (path, mtime) and kept in an LRU."""

    def __init__(self, size: int = 4096) -> None:
        self.size = size
        self._items: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, path: Path) -> bytes | None:
        from seedbox2.analysis import waveform

        try:
            key = (str(path), path.stat().st_mtime_ns)
        except OSError:
            return None
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        try:
            wf = waveform.Waveform.from_bytes(gzip.decompress(path.read_bytes()))
        except (OSError, ValueError, EOFError) as e:  # gzip.BadGzipFile is an OSError, WaveformError a ValueError
            import logging

            logging.getLogger(__name__).warning("unreadable waveform %s: %s", path, e)
            return None
        data = wf.overview[:, 0, :].tobytes()  # [bands, columns] peaks
        with self._lock:
            self._items[key] = data
            while len(self._items) > self.size:
                self._items.popitem(last=False)
        return data


def pack_overviews(rows: list[bytes | None], *, columns: int, bands: int) -> bytes:
    out = [struct.pack(OVERVIEW_HEADER, OVERVIEW_MAGIC, columns, bands, len(rows))]
    for r in rows:
        if r is None or len(r) != columns * bands:
            out.append(b"\0")
        else:
            out += [b"\1", r]
    return b"".join(out)
