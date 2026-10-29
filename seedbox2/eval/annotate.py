"""Annotations for our own corpus: export an analysis to an editable file, correct it, read the corrections back.

The corpus (`$SEEDBOX2_CORPUS`) is a folder of audio files, each with a `<name>.json` next to it:

    {
      "format": "seedbox2-annotation/1",
      "verified": false,                 # set to true once a person has checked every field
      "duration_s": 245.3,
      "bpm": 126.0,
      "key": "A minor",                  # or "Am", "8A"; null when unknown
      "grid": {"first_downbeat_s": 0.42, "bpm": 126.0},   # a constant-tempo beat grid, or instead
      "beats": [0.42, 0.896, ...], "downbeats": [0.42, 2.325, ...],   # explicit lists (they win over "grid")
      "sections": [{"start_s": 0.42, "label": "intro"}, ..., {"start_s": 240.1, "label": "end"}],
      "vocals": [[61.4, 92.0], [153.6, 184.1]],          # sung spans; [] for an instrumental
      "cues": [{"time_s": 0.42, "label": "first downbeat"}]   # not scored, for Rekordbox
    }

A field that is missing or null is not scored. Rekordbox users can export to a collection XML instead (beat grid,
key and a memory cue per section start), fix the grid and the cues in Rekordbox, export the collection and read it
back with `import-rekordbox`: TEMPO marks become the grid, memory cues named after a section become the sections.
"""

from __future__ import annotations

import json
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

from .analysers import Estimate, grid
from .datasets import Reference, normalise_key

FORMAT = "seedbox2-annotation/1"


def _is_grid(beats: list[float], downbeats: list[float], bpm: float | None) -> bool:
    if not bpm or len(beats) < 3 or not downbeats:
        return False
    period = 60.0 / bpm
    beats_ok = all(abs((b - beats[0]) / period - round((b - beats[0]) / period)) < 0.01 for b in beats)
    return beats_ok and abs(beats[1] - beats[0] - period) < 0.005


def to_annotation(est: Estimate) -> dict:
    """An editable annotation of an analysis (unverified)."""
    ann: dict = {"format": FORMAT, "verified": False, "duration_s": round(est.duration_s, 3), "bpm": est.bpm,
                 "key": est.key}
    if est.beats is not None and est.downbeats and _is_grid(est.beats, est.downbeats, est.bpm):
        ann["grid"] = {"first_downbeat_s": round(est.downbeats[0], 4), "bpm": est.bpm}
    else:
        ann["beats"], ann["downbeats"] = est.beats, est.downbeats
    ann["sections"] = None if est.sections is None else [{"start_s": round(t, 3), "label": label}
                                                         for t, label in est.sections]
    ann["vocals"] = None if est.vocals is None else [[round(a, 3), round(b, 3)] for a, b in est.vocals]
    ann["cues"] = [{"time_s": round(t, 3), "label": label} for t, label in est.cues or []]
    return ann


def write_annotation(path: Path, ann: dict) -> None:
    path.write_text(json.dumps(ann, indent=2) + "\n", encoding="utf-8")


def reference_from(ann: dict, audio: Path, dataset: str = "corpus", track: str = "") -> Reference:
    if ann.get("format", FORMAT) != FORMAT:
        raise ValueError(f"not a {FORMAT} file")
    duration = ann.get("duration_s")
    beats, downbeats = ann.get("beats"), ann.get("downbeats")
    g = ann.get("grid")
    if beats is None and g:
        if not duration:
            raise ValueError("a grid needs duration_s")
        beats, gdown = grid(float(g["first_downbeat_s"]), float(g["bpm"]), float(duration),
                            int(g.get("beats_per_bar", 4)))
        downbeats = gdown if downbeats is None else downbeats
    secs = ann.get("sections")
    vocals = ann.get("vocals")
    return Reference(
        dataset=dataset, track=track or audio.stem, audio=audio,
        bpm=ann.get("bpm") or (g and float(g["bpm"])) or None,
        key=normalise_key(ann.get("key")),
        beats=None if beats is None else [float(b) for b in beats],
        downbeats=None if downbeats is None else [float(b) for b in downbeats],
        sections=None if secs is None else [(float(s["start_s"]), str(s["label"])) for s in secs],
        vocals=None if vocals is None else [(float(a), float(b)) for a, b in vocals],
        duration_s=duration, extra={"verified": bool(ann.get("verified"))})


def read_annotation(path: Path, audio: Path | None = None) -> Reference:
    ann = json.loads(path.read_text(encoding="utf-8"))
    return reference_from(ann, audio or path.with_suffix(".mp3"))


# ---------------------------------------------------------------------------------------------------------------
# Rekordbox collection XML

_RB_KEYS = {"major": "", "minor": "m"}


def _rb_key(key: str | None) -> str:
    if not key:
        return ""
    tonic, mode = key.split()
    return tonic + _RB_KEYS.get(mode, "")


def _location(audio: Path) -> str:
    return "file://localhost" + urllib.parse.quote(audio.resolve().as_posix())


def _path_of(location: str) -> Path:
    p = urllib.parse.urlparse(location)
    return Path(urllib.parse.unquote(p.path))


def to_rekordbox(items: list[tuple[Path, dict]]) -> str:
    """A Rekordbox collection XML of annotations: the grid as one TEMPO mark, a memory cue per section start."""
    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(root, "PRODUCT", Name="Seedbox 2", Version="eval", Company="Seedbox")
    coll = ET.SubElement(root, "COLLECTION", Entries=str(len(items)))
    for i, (audio, ann) in enumerate(items, 1):
        ref = reference_from(ann, audio)
        t = ET.SubElement(coll, "TRACK", TrackID=str(i), Name=audio.stem, Location=_location(audio),
                          TotalTime=str(int(round(ann.get("duration_s") or 0))),
                          AverageBpm=f"{ref.bpm or 0:.2f}", Tonality=_rb_key(ref.key))
        if ref.downbeats and ref.bpm:
            ET.SubElement(t, "TEMPO", Inizio=f"{ref.downbeats[0]:.3f}", Bpm=f"{ref.bpm:.2f}", Metro="4/4", Battito="1")
        for start, label in ref.sections or []:
            ET.SubElement(t, "POSITION_MARK", Name=label, Type="0", Start=f"{start:.3f}", Num="-1")
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def from_rekordbox(xml_text: str) -> dict[Path, dict]:
    """Corrections from a Rekordbox collection XML, by audio path, as partial annotations (only what it holds)."""
    out: dict[Path, dict] = {}
    for t in ET.fromstring(xml_text).iter("TRACK"):
        if "Location" not in t.attrib:
            continue  # a playlist entry
        ann: dict = {}
        if total := float(t.get("TotalTime") or 0):
            ann["duration_s"] = total
        if key := normalise_key(t.get("Tonality")):
            ann["key"] = key
        tempos = sorted(((float(m.get("Inizio", 0)), float(m.get("Bpm", 0)), int(m.get("Battito", 1)))
                         for m in t.iter("TEMPO")), key=lambda m: m[0])
        if tempos:
            ann["bpm"] = round(tempos[0][1], 3)
            if len({round(b, 2) for _, b, _ in tempos}) == 1:
                start, bpm, beat = tempos[0]
                first = start + ((1 - beat) % 4) * 60.0 / bpm  # the mark's downbeat (Battito 1-4 = beat in bar)
                ann["grid"] = {"first_downbeat_s": round(first, 4), "bpm": bpm}
            elif total:
                ann["beats"], ann["downbeats"] = _piecewise(tempos, total)
        marks = sorted((float(m.get("Start", 0)), m.get("Name") or "") for m in t.iter("POSITION_MARK")
                       if m.get("Num") == "-1")
        if marks:
            ann["sections"] = [{"start_s": s, "label": n or "section"} for s, n in marks]
        out[_path_of(t.get("Location", "")).resolve()] = ann
    return out


def _piecewise(tempos: list[tuple[float, float, int]], total: float) -> tuple[list[float], list[float]]:
    beats, downbeats = [], []
    for i, (start, bpm, beat) in enumerate(tempos):
        stop = tempos[i + 1][0] if i + 1 < len(tempos) else total
        t, n = start, beat
        while t < stop - 1e-6:
            beats.append(round(t, 4))
            if n == 1:
                downbeats.append(round(t, 4))
            t, n = t + 60.0 / bpm, n % 4 + 1
    return beats, downbeats


def merge(ann: dict, corrections: dict) -> dict:
    """An annotation with Rekordbox's corrections applied; the grid replaces explicit beat lists and vice versa."""
    out = dict(ann, **corrections)
    if "grid" in corrections:
        out.pop("beats", None)
        out.pop("downbeats", None)
    elif "beats" in corrections:
        out.pop("grid", None)
    return out
