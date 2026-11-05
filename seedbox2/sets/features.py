"""What Seedbox 2's own analysis says about the tracks of a set. Nothing here comes from tunebat or file tags, except the
genre, which no analysis gives: a tag's key or BPM isn't ours (`seedbox2/analysis/key.py`), so it counts as unknown."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from seedbox2.analysis import vibe

log = logging.getLogger(__name__)

VOCALS_ANALYSER = "vocals"  # #47's analysis; a track without stems has none, and its vocal heaviness is unknown
OURS = {"analysis", "user"}


@dataclass
class TrackFeatures:
    """One track, with None for whatever isn't known. `vibes` maps a CLAP window's name to its unit vector."""

    hash: str
    title: str = ""
    artist: str = ""
    bpm: float | None = None
    camelot: str | None = None
    energy: float | None = None  # 0-1, as `energy.analyse` reports it: (rating - 1) / 9
    vocals: float | None = None  # the share of the beats that are sung, 0-1
    genre: str | None = None
    vibes: dict[str, np.ndarray] = field(default_factory=dict)


def vocal_share(data: Any) -> float | None:
    """The share of a track's beats that have vocals, from a `vocals` analysis."""
    active = data.get("active") if isinstance(data, dict) else None
    if not isinstance(active, str) or not active:
        return None
    return active.count("1") / len(active)


def _vibes(lib: Any, hash_: str) -> dict[str, np.ndarray]:
    path: Path | None = vibe.current(lib, hash_)
    if path is None:
        return {}
    try:
        data = vibe.read_embeddings(path)
    except (OSError, ValueError, KeyError) as e:
        log.warning("skipping the embeddings of %s: %s", hash_, e)
        return {}
    return dict(zip(data["names"], data["vectors"], strict=True))


def add_detail(lib: Any, track: TrackFeatures) -> None:
    """What `load_features(detail=False)` left out: the vocals analysis and the embeddings, one query and one file."""
    vocals = lib.analysis(track.hash, VOCALS_ANALYSER)
    track.vocals = vocal_share(vocals["data"]) if vocals else None
    track.vibes = _vibes(lib, track.hash)


def load_features(lib: Any, hashes: Iterable[str] | None, *, detail: bool = True) -> dict[str, TrackFeatures]:
    """The features of these tracks (None: the whole library), by hash. An unknown track has no entry. Without `detail`
    they have no vocals or embeddings, which a large library can't afford to read for every track."""
    out: dict[str, TrackFeatures] = {}
    for row in lib.view_rows(hashes=hashes):
        h = row["hash"]
        out[h] = TrackFeatures(
            hash=h, title=row.get("title") or "", artist=row.get("artist") or "",
            bpm=row["bpm"] if row.get("bpm_from") == "analysis" else None,
            camelot=row["key"] if row.get("key_from") in OURS else None,
            energy=row["energy"] if row.get("energy_from") == "analysis" else None,
            genre=row.get("genre") or None,
        )  # fmt: skip
        if detail:
            add_detail(lib, out[h])
    return out
