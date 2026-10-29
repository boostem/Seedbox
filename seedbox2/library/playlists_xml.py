"""Import Rekordbox XML and iTunes/Music library XML playlists as crates. Read-only on the XML and the music.

- **Rekordbox** (File > Export Collection in xml format): `COLLECTION/TRACK` (TrackID, Location as a file URL,
  AverageBpm, Tonality, Comments...) with `TEMPO` (the beatgrid) and `POSITION_MARK` (memory cues: Num -1; hot
  cues: Num 0-7; loops: Type 4 with an End) children; `PLAYLISTS` is a tree of `NODE`s, folders (Type 0) and
  playlists (Type 1) whose `TRACK Key` is a TrackID (KeyType 0) or a Location (KeyType 1).
- **iTunes / Music** (File > Library > Export Library): a plist with `Tracks` (by Track ID, with Location, BPM,
  Comments...) and `Playlists` (with Folder, Parent Persistent ID and Playlist Items). The library's own lists
  (Library, Music, Downloaded...: `Master` or `Distinguished Kind`) and folders are not crates.

Every playlist becomes a crate (source `rekordbox` or `itunes`) named by its path in the tree ("Folder / List"),
in the playlist's order; importing again updates the same crates. The files go through the Importer, so they are
hashed and tagged like any other. What the XML says about a track (BPM, key, comment, rating) is a hint, kept in
`tracks.tags["rekordbox"]` / `["itunes"]`; Rekordbox's cues and beatgrid are kept as the track's `rekordbox`
analysis, for the cue point work to learn from (#55).
"""

from __future__ import annotations

import logging
import plistlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree as ET

from seedbox2.library.importer import Importer, Progress
from seedbox2.library.repo import Library

log = logging.getLogger(__name__)

REKORDBOX_ANALYSIS = "rekordbox"
REKORDBOX_ANALYSIS_VERSION = "xml-1"
CUE_TYPES = {"0": "cue", "1": "fade-in", "2": "fade-out", "3": "load", "4": "loop"}


class NotAPlaylistFile(ValueError):
    """Neither a Rekordbox XML nor an iTunes/Music library XML."""


@dataclass
class XmlTrack:
    path: Path
    hints: dict[str, Any] = field(default_factory=dict)
    cues: list[dict] = field(default_factory=list)
    tempo: list[dict] = field(default_factory=list)


@dataclass
class XmlLibrary:
    source: str  # rekordbox, itunes
    tracks: dict[str, XmlTrack]  # by the file's own track ID
    playlists: list[tuple[str, list[str]]]  # (path in the tree, track IDs in order)


@dataclass
class XmlImportReport:
    source: str = ""
    crates: int = 0
    tracks: int = 0
    added: int = 0
    cues: int = 0  # tracks that came with cue points
    missing: int = 0  # tracks whose file isn't there
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"source": self.source, "crates": self.crates, "tracks": self.tracks, "added": self.added,
                "cues": self.cues, "missing": self.missing, "errors": self.errors[:50],
                "error_count": len(self.errors)}  # fmt: skip


def file_url_to_path(url: str, platform: str = sys.platform) -> Path | None:
    """`file://localhost/Users/me/a%20b.mp3` (or `file:///C:/Music/x.mp3` on Windows) as a path; None if not a file
    URL."""
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if platform == "win32":
        if len(path) > 2 and path[0] == "/" and path[2] == ":":  # /C:/Music
            path = path[1:]
        elif parsed.netloc not in ("", "localhost"):  # a network share: file://server/share/x.mp3
            path = f"//{parsed.netloc}{path}"
        path = path.replace("/", "\\")
    return Path(path)


def _num(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None  # type: ignore[arg-type]
    except ValueError:
        return None


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None and v != ""}


def parse_rekordbox(root: ET.Element) -> XmlLibrary:
    tracks: dict[str, XmlTrack] = {}
    by_location: dict[str, str] = {}
    for t in root.iterfind("COLLECTION/TRACK"):
        location, track_id = t.get("Location", ""), t.get("TrackID", "")
        path = file_url_to_path(location)
        if path is None or not track_id:
            continue
        hints = _clean({"title": t.get("Name"), "artist": t.get("Artist"), "bpm": _num(t.get("AverageBpm")),
                        "key": t.get("Tonality"), "comment": t.get("Comments"), "genre": t.get("Genre"),
                        "label": t.get("Label"), "rating": int(_num(t.get("Rating")) or 0) // 51 or None,
                        "color": t.get("Colour")})  # fmt: skip
        tempo = [_clean({"start": _num(m.get("Inizio")), "bpm": _num(m.get("Bpm")), "meter": m.get("Metro"),
                         "beat": int(_num(m.get("Battito")) or 1)}) for m in t.iterfind("TEMPO")]  # fmt: skip
        cues = []
        for m in t.iterfind("POSITION_MARK"):
            start = _num(m.get("Start"))
            if start is None:
                continue
            num = int(_num(m.get("Num")) or 0) if m.get("Num") not in (None, "") else -1
            cue = {"start": start, "type": CUE_TYPES.get(m.get("Type", "0"), "cue"),
                   "hot_cue": num if num >= 0 else None, "name": m.get("Name") or ""}  # fmt: skip
            end = _num(m.get("End"))
            if end is not None:
                cue["end"] = end
            cues.append(cue)
        tracks[track_id] = XmlTrack(path, hints, sorted(cues, key=lambda c: c["start"]), tempo)
        by_location[location] = track_id
    playlists: list[tuple[str, list[str]]] = []

    def walk(node: ET.Element, trail: list[str]) -> None:
        for child in node.iterfind("NODE"):
            name = child.get("Name", "")
            if child.get("Type") == "1":
                by_key = child.get("KeyType", "0") == "1"
                ids = []
                for t in child.iterfind("TRACK"):
                    key = t.get("Key", "")
                    track_id = by_location.get(key) if by_key else key
                    if track_id in tracks:
                        ids.append(track_id)
                playlists.append((" / ".join([*trail, name]), ids))
            else:
                walk(child, [*trail, name])

    for top in root.iterfind("PLAYLISTS/NODE"):  # the ROOT folder
        walk(top, [])
    return XmlLibrary("rekordbox", tracks, playlists)


def parse_itunes(plist: dict) -> XmlLibrary:
    tracks: dict[str, XmlTrack] = {}
    for track_id, t in (plist.get("Tracks") or {}).items():
        path = file_url_to_path(str(t.get("Location", "")))
        if path is None:  # streamed (Apple Music) or a missing file
            continue
        hints = _clean({"title": t.get("Name"), "artist": t.get("Artist"), "bpm": t.get("BPM"),
                        "comment": t.get("Comments"), "genre": t.get("Genre"), "year": t.get("Year"),
                        "rating": int(t.get("Rating") or 0) // 20 or None})  # fmt: skip
        tracks[str(track_id)] = XmlTrack(path, hints)
    lists = plist.get("Playlists") or []
    names = {p.get("Playlist Persistent ID"): (p.get("Name", ""), p.get("Parent Persistent ID")) for p in lists}

    def trail(parent: str | None) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        while parent and parent in names and parent not in seen:
            seen.add(parent)
            name, parent = names[parent]
            out.insert(0, name)
        return out

    playlists = []
    for p in lists:
        if p.get("Master") or p.get("Distinguished Kind") or p.get("Folder") or p.get("Visible") is False:
            continue
        ids = [str(i.get("Track ID")) for i in p.get("Playlist Items") or []]
        playlists.append((" / ".join([*trail(p.get("Parent Persistent ID")), p.get("Name", "")]),
                          [i for i in ids if i in tracks]))  # fmt: skip
    return XmlLibrary("itunes", tracks, playlists)


def parse(path: Path | str) -> XmlLibrary:
    """Read a Rekordbox XML or an iTunes/Music library XML. NotAPlaylistFile if it is neither; OSError if it can't
    be read."""
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(4096)
    if b"<plist" in head or b"PropertyList" in head:
        try:
            with open(path, "rb") as f:
                plist = plistlib.load(f)
        except Exception as e:  # noqa: BLE001 - plistlib raises several types
            raise NotAPlaylistFile(f"{path.name}: not a readable library XML ({e})") from e
        if not isinstance(plist, dict) or "Tracks" not in plist:
            raise NotAPlaylistFile(f"{path.name}: an XML plist, but not an iTunes/Music library")
        return parse_itunes(plist)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        raise NotAPlaylistFile(f"{path.name}: not XML ({e})") from e
    if root.tag != "DJ_PLAYLISTS":
        raise NotAPlaylistFile(f"{path.name}: neither a Rekordbox XML nor an iTunes/Music library XML")
    return parse_rekordbox(root)


def import_playlists(lib: Library, importer: Importer, path: Path | str,
                     progress: Progress | None = None) -> XmlImportReport:  # fmt: skip
    """Import the playlists of a Rekordbox or iTunes/Music XML as crates, with the files they list."""
    xml = parse(path)
    report = XmlImportReport(source=xml.source)
    wanted = list(dict.fromkeys(tid for _, ids in xml.playlists for tid in ids))
    present = []
    for tid in wanted:
        if xml.tracks[tid].path.is_file():
            present.append(tid)
        else:
            report.missing += 1
            report.errors.append(f"not found: {xml.tracks[tid].path}")
    files = importer.import_paths([xml.tracks[tid].path for tid in present], source=xml.source, progress=progress)
    report.added = files.added
    report.errors += files.errors
    hash_of: dict[str, str] = {}
    for tid in present:
        t = xml.tracks[tid]
        track = lib.track_for_path(t.path)
        if track is None:  # couldn't be decoded: in the errors already
            continue
        hash_of[tid] = track["hash"]
        lib.upsert_track(track["hash"], track["duration"], tags={xml.source: t.hints})
        if t.cues or t.tempo:
            lib.put_analysis(track["hash"], REKORDBOX_ANALYSIS, REKORDBOX_ANALYSIS_VERSION,
                             {"cues": t.cues, "tempo": t.tempo})  # fmt: skip
            report.cues += bool(t.cues)
    report.tracks = len(hash_of)
    for name, ids in xml.playlists:
        lib.save_crate(name, [hash_of[i] for i in ids if i in hash_of], folder=name, source=xml.source)
        report.crates += 1
    log.info("imported %s playlists from %s: %s", xml.source, Path(path).name, report.to_json())
    return report
