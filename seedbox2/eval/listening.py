"""The blind A/B listening test behind `#/listen` (#86): which renders to compare, the results, pooling them.

Metrics don't hear, so people rate transitions. Renders are plain audio files, so any source fits, a human DJ's
version included:

    <data folder>/listening/<pair>/<version>[--<style>].<wav|mp3|m4a|aac|flac|ogg|opus>

    listening/house into house/seedbox1-0.3.0--bass swap.wav
    listening/house into house/seedbox1-0.3.0--echo out.wav      an alternative plan
    listening/house into house/human--DJ Someone.mp3

`python -m seedbox2.eval listen [pairs.toml] [--techniques "bass swap,echo out"]` renders them with our engines.

A trial is two renders of one pair, the comparison with the fewest ratings, in random order. The client only ever
sees the trial's opaque id and the slots "a" and "b"; what they were is stored with the answer. One row in the
`ratings` table per answer: test = TEST, subject = the pair, choice = the winning slot, data = see `make_rating`.

Summary: win rates with Wilson 95% intervals, per engine version and per style. A trial only counts for a table when
its two renders differ there: seedbox1's bass swap against its echo out says something about styles, nothing about
seedbox1. Answers from one person are not fully independent, so read the intervals as a little too narrow.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import secrets
import sys
import threading
import uuid
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

TEST = "ab-transition"
FORMAT = "seedbox2-listening-ratings"
FORMAT_VERSION = 1
FOLDER = "listening"
SCORES = ("smoothness", "energy", "dj")  # each 1-5; dj is "would a DJ do this"
SLOTS = ("a", "b")
ANONYMOUS = "anonymous"
DEFAULT_STYLE = "default"
AUDIO_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
               ".flac": "audio/flac", ".ogg": "audio/ogg", ".opus": "audio/ogg"}  # fmt: skip
Z_95 = 1.959964
MAX_TEXT = 2000
MAX_NAME = 200


@dataclass(frozen=True)
class Version:
    """One render of a pair."""

    pair: str
    version: str
    style: str
    path: Path

    @property
    def label(self) -> str:
        return label(self.version, self.style)

    @property
    def media_type(self) -> str:
        return AUDIO_TYPES[self.path.suffix.lower()]

    def describe(self) -> dict[str, str]:
        return {"version": self.version, "style": self.style}


def label(version: str, style: str) -> str:
    return f"{version}--{style}" if style else version


def scan(folder: Path) -> list[Version]:
    """Every render in the folder, sorted. A second file with the same name but another extension is ignored."""
    if not folder.is_dir():
        return []
    out: dict[tuple[str, str], Version] = {}
    for pair_dir in sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for f in sorted(pair_dir.iterdir()):
            if f.name.startswith(".") or not f.is_file() or f.suffix.lower() not in AUDIO_TYPES:
                continue
            version, _, style = f.stem.partition("--")
            if version.strip():
                v = Version(pair_dir.name, version.strip(), style.strip(), f)
                out.setdefault((v.pair, v.label), v)
    return list(out.values())


def comparisons(versions: Iterable[Version]) -> list[tuple[Version, Version]]:
    """Every two renders of the same pair."""
    by_pair: dict[str, list[Version]] = defaultdict(list)
    for v in versions:
        by_pair[v.pair].append(v)
    return [c for pair in by_pair.values() for c in combinations(pair, 2)]


def _comparison_key(pair: str, x: Mapping[str, str], y: Mapping[str, str]) -> tuple[str, frozenset[str]]:
    return pair, frozenset((label(x["version"], x.get("style", "")), label(y["version"], y.get("style", ""))))


def rater_of(data: Mapping[str, Any]) -> str:
    return str(data.get("rater") or ANONYMOUS)


def progress(versions: Iterable[Version], ratings: Iterable[Mapping[str, Any]], rater: str) -> dict[str, int]:
    """How many of the comparisons this rater has answered at least once."""
    keys = {_comparison_key(x.pair, x.describe(), y.describe()) for x, y in comparisons(versions)}
    mine = {_comparison_key(r["subject"], r["data"]["a"], r["data"]["b"]) for r in ratings
            if rater_of(r["data"]) == (rater or ANONYMOUS)}  # fmt: skip
    return {"done": len(keys & mine), "total": len(keys)}


def pick(versions: Iterable[Version], ratings: Iterable[Mapping[str, Any]], rater: str = "",
         rng: random.Random | None = None) -> tuple[Version, Version] | None:  # fmt: skip
    """The next two renders to compare, in random order: what this rater has heard least, then what everyone has."""
    rng = rng or random.SystemRandom()
    mine: dict[Any, int] = defaultdict(int)
    everyone: dict[Any, int] = defaultdict(int)
    for r in ratings:
        key = _comparison_key(r["subject"], r["data"]["a"], r["data"]["b"])
        everyone[key] += 1
        if rater_of(r["data"]) == (rater or ANONYMOUS):
            mine[key] += 1

    def rank(c: tuple[Version, Version]) -> tuple[int, int, float]:
        key = _comparison_key(c[0].pair, c[0].describe(), c[1].describe())
        return mine[key], everyone[key], rng.random()

    options = comparisons(versions)
    if not options:
        return None
    first, second = min(options, key=rank)
    return (first, second) if rng.random() < 0.5 else (second, first)


# --- trials ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Trial:
    id: str
    a: Version
    b: Version

    def slot(self, name: str) -> Version:
        return self.a if name == "a" else self.b


class Trials:
    """The trials handed out and not answered yet, in memory: restarting the server just deals a new one. Who is who
    stays here, not in the client."""

    MAX = 64

    def __init__(self) -> None:
        self._items: OrderedDict[str, Trial] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, a: Version, b: Version) -> Trial:
        trial = Trial(secrets.token_urlsafe(12), a, b)
        with self._lock:
            self._items[trial.id] = trial
            while len(self._items) > self.MAX:
                self._items.popitem(last=False)
        return trial

    def get(self, trial_id: str) -> Trial | None:
        with self._lock:
            return self._items.get(trial_id)

    def take(self, trial_id: str) -> Trial | None:
        """Removes it, so one trial can't be answered twice."""
        with self._lock:
            return self._items.pop(trial_id, None)


# --- ratings ---------------------------------------------------------------------------------------------------------


def make_rating(trial: Trial, choice: str, scores: Mapping[str, Mapping[str, int]] | None = None, comment: str = "",
                rater: str = "") -> dict[str, Any]:  # fmt: skip
    """The row for `Library.add_ratings`. `uid` is what makes importing the same file twice harmless."""
    return {"subject": trial.a.pair, "choice": choice, "data": {
        "uid": uuid.uuid4().hex,
        "rater": rater.strip() or ANONYMOUS,
        "a": trial.a.describe(),
        "b": trial.b.describe(),
        "scores": {k: dict(v) for k, v in (scores or {}).items() if v},
        "comment": comment.strip(),
    }}  # fmt: skip


def wilson(wins: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """The Wilson score interval: unlike the normal approximation it behaves at 0/5 and 5/5."""
    if n <= 0:
        return 0.0, 1.0
    p = wins / n
    denominator = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denominator), min(1.0, (centre + margin) / denominator)


def _rate(wins: int, n: int) -> dict[str, Any]:
    low, high = wilson(wins, n)
    return {"trials": n, "wins": wins, "win_rate": round(wins / n, 4) if n else None, "ci_low": round(low, 4),
            "ci_high": round(high, 4)}  # fmt: skip


def _style_of(side: Mapping[str, str]) -> str:
    return side.get("style") or DEFAULT_STYLE


def _table(rows: list[Mapping[str, Any]], name_of: Callable[[Mapping[str, str]], str]) -> list[dict[str, Any]]:
    wins: dict[str, int] = defaultdict(int)
    trials: dict[str, int] = defaultdict(int)
    scores: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        data = r["data"]
        names = {slot: name_of(data[slot]) for slot in SLOTS}
        for slot in SLOTS:
            for score, value in data.get("scores", {}).get(slot, {}).items():
                scores[names[slot]][score].append(value)
        if names["a"] == names["b"]:
            continue
        trials[names["a"]] += 1
        trials[names["b"]] += 1
        wins[names[r["choice"]]] += 1
    table = [{"name": name, **_rate(wins[name], trials[name]),
              "scores": {s: round(sum(v) / len(v), 2) for s, v in scores[name].items()}} for name in trials]  # fmt: skip
    return sorted(table, key=lambda row: (-row["win_rate"], -row["trials"], row["name"]))


def _matchups(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each two engine versions head to head, since a win rate alone depends on who the opponents were."""
    tally: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        a, b = r["data"]["a"]["version"], r["data"]["b"]["version"]
        if a == b:
            continue
        x, y = sorted((a, b))
        tally[(x, y)][0] += 1
        tally[(x, y)][1] += (a if r["choice"] == "a" else b) == x
    return [{"name": x, "opponent": y, **_rate(wins, n)} for (x, y), (n, wins) in sorted(tally.items())]


def summary(ratings: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(ratings)
    return {
        "ratings": len(rows),
        "raters": len({rater_of(r["data"]) for r in rows}),
        "versions": _table(rows, lambda side: side["version"]),
        "styles": _table(rows, _style_of),
        "matchups": _matchups(rows),
        # how often the render played as A won: should be near 50%, or the order is biasing people
        "slot_a": _rate(sum(r["choice"] == "a" for r in rows), len(rows)),
    }


# --- pooling results from several people -----------------------------------------------------------------------------


def export(ratings: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Ratings only: no audio and no paths, so the file is safe to pass around."""
    return {"format": FORMAT, "version": FORMAT_VERSION, "ratings": [
        {"subject": r["subject"], "choice": r["choice"], "created_at": r.get("created_at"), "data": r["data"]}
        for r in ratings]}  # fmt: skip


def save_export(folder: Path, ratings: Iterable[Mapping[str, Any]], rater: str = "") -> Path:
    """The export as a file in the listening folder (scan only looks in its subfolders), one per save."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"ratings-{_safe_name(rater or ANONYMOUS)}-{stamp}.json"
    out.write_text(json.dumps(export(ratings), indent=1, ensure_ascii=False), encoding="utf-8")
    return out


def _name(value: Any, what: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > MAX_NAME or not (value.strip() or allow_empty):
        raise ValueError(f"{what} must be a short text")
    return value


def _clean_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping) or not isinstance(row.get("data"), Mapping):
        raise ValueError("a rating must be an object with data")
    data = row["data"]
    if row.get("choice") not in SLOTS:
        raise ValueError(f"choice must be one of {', '.join(SLOTS)}")
    sides = {}
    for slot in SLOTS:
        side = data.get(slot)
        if not isinstance(side, Mapping):
            raise ValueError(f"data.{slot} must say which render it was")
        sides[slot] = {"version": _name(side.get("version"), "version"),
                       "style": _name(side.get("style", ""), "style", allow_empty=True)}  # fmt: skip
    scores = data.get("scores", {})
    if not isinstance(scores, Mapping) or set(scores) - set(SLOTS):
        raise ValueError("scores must be per slot")
    clean_scores = {}
    for slot, values in scores.items():
        if not isinstance(values, Mapping) or set(values) - set(SCORES):
            raise ValueError(f"scores are {', '.join(SCORES)}")
        for v in values.values():
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 5:
                raise ValueError("scores go from 1 to 5")
        if values:
            clean_scores[slot] = dict(values)
    comment = data.get("comment", "")
    if not isinstance(comment, str) or len(comment) > MAX_TEXT:
        raise ValueError(f"comment must be a text of at most {MAX_TEXT} characters")
    created_at = row.get("created_at")
    if created_at is not None and not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", str(created_at)):
        raise ValueError("created_at must look like 2026-11-04T12:00:00Z")
    return {"subject": _name(row.get("subject"), "subject"), "choice": row["choice"], "created_at": created_at,
            "data": {"uid": _name(data.get("uid"), "uid"), "rater": _name(data.get("rater", ANONYMOUS), "rater"),
                     **sides, "scores": clean_scores, "comment": comment}}  # fmt: skip


def parse_import(doc: Any, existing: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """(the rows to add, how many were already here). Anything malformed raises ValueError: all or nothing, so a bad
    file can't leave half its ratings behind."""
    if not isinstance(doc, Mapping) or doc.get("format") != FORMAT:
        raise ValueError("not a Seedbox 2 ratings file")
    if doc.get("version") != FORMAT_VERSION:
        raise ValueError(f"unsupported ratings file version {doc.get('version')!r}")
    if not isinstance(doc.get("ratings"), list):
        raise ValueError("the file has no list of ratings")
    seen = {r["data"].get("uid") for r in existing}
    rows, duplicates = [], 0
    for i, raw in enumerate(doc["ratings"]):
        try:
            row = _clean_row(raw)
        except ValueError as e:
            raise ValueError(f"rating {i + 1}: {e}") from None
        if row["data"]["uid"] in seen:
            duplicates += 1
        else:
            seen.add(row["data"]["uid"])
            rows.append(row)
    return rows, duplicates


# --- rendering: python -m seedbox2.eval listen ------------------------------------------------------------------------


def _safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", name).strip(" .") or "untitled"


def engine_versions() -> dict[str, str]:
    import seedbox
    import seedbox2

    return {"seedbox1": f"seedbox1-{seedbox.__version__}", "seedbox2": f"seedbox2-{seedbox2.__version__}"}


def write_render(folder: Path, pair: str, version: str, style: str, mix: Any, sr: int) -> Path:
    """16-bit PCM, which every webview can play (float WAVs aren't a given)."""
    import numpy as np
    from scipy.io import wavfile

    out = folder / _safe_name(pair) / f"{_safe_name(label(version, style))}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(np.ascontiguousarray(mix.T), -1.0, 1.0) * 32767).astype(np.int16)
    wavfile.write(out, sr, pcm)
    return out


def render(pairs: Iterable[Any], engines: Iterable[str], folder: Path, techniques: Iterable[str] = (),
           log: Callable[[str], None] = lambda s: None) -> list[Path]:  # fmt: skip
    """Every pair with every engine, once with the engine's own choice and once per forced technique (the alternative
    plans). A forced technique the engine would have picked anyway lands on the same file."""
    from .mixes import ENGINES, EngineUnavailable

    names = engine_versions()
    written = []
    for pair in pairs:
        for engine in engines:
            for technique in [None, *techniques]:
                try:
                    r = ENGINES[engine](replace(pair, technique=technique) if technique else pair)
                except EngineUnavailable as e:
                    log(f"{pair.name}: skipping {engine}: {e}")
                    break
                except (ValueError, OSError) as e:
                    log(f"{pair.name}: {engine} {technique or 'default'} failed: {e}")
                    continue
                style = str((r.plan or {}).get("technique") or technique or "")
                written.append(write_render(folder, pair.name, names[engine], style, r.mix, r.sr))
                log(f"{pair.name}: {written[-1].name}")
    return written


def main(argv: list[str] | None = None) -> int:
    from seedbox2.library import default_path

    from . import mixes

    p = argparse.ArgumentParser(prog="python -m seedbox2.eval listen",
                                description="Render pairs into the folder the #/listen page plays.")  # fmt: skip
    p.add_argument("pairs", nargs="?", type=Path, help="pairs.toml (default: the built-in synthetic pairs)")
    p.add_argument("--engines", default=",".join(mixes.ENGINES), help=f"comma-separated (default: {','.join(mixes.ENGINES)})")
    p.add_argument("--techniques", default="", help='also render these as alternative plans, e.g. "bass swap,echo out"')
    p.add_argument("--folder", type=Path, help=f"default: {FOLDER}/ next to the library")
    p.add_argument("--work", type=Path, help="where the synthetic tracks go (default: a temporary folder)")
    args = p.parse_args(argv)
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    bad = [e for e in engines if e not in mixes.ENGINES]
    if bad:
        p.error(f"unknown engine(s) {', '.join(bad)}; choose from {', '.join(mixes.ENGINES)}")
    techniques = [t.strip() for t in args.techniques.split(",") if t.strip()]
    folder = args.folder or default_path().parent / FOLDER
    import tempfile
    import tomllib

    with tempfile.TemporaryDirectory(prefix="seedbox2-listen-") as tmp:
        try:
            pairs = mixes.load_pairs(args.pairs) if args.pairs else mixes.synthetic_pairs(args.work or Path(tmp))
        except (ValueError, OSError, tomllib.TOMLDecodeError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        written = render(pairs, engines, folder, techniques, log=lambda s: print(s, file=sys.stderr))
    print(f"{len(set(written))} render(s) in {folder}")
    return 0 if written else 1
