"""Exporting a set as one continuous mix (#70): the `set_export` job. The DSP is in `seedbox2.mix.setrender` and `setexport`.

The heavy pool runs it, and the job's three stages are one bar: the render, the passes that find the make-up, the encode.
A job the app didn't finish is run again at the next start and picks up its spool at the last finished track. A cancel
throws the spool away; a failure keeps it, so trying again doesn't start over.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from seedbox2 import paths
from seedbox2.analysis import energy, loudness, vibe
from seedbox2.jobs import register
from seedbox2.jobs.worker import Cancelled
from seedbox2.mix import convert, offline, render, setexport
from seedbox2.mix import master as master_chain
from seedbox2.mix.plan import MixPlan, fold_stem_lanes
from seedbox2.mix.setchain import plan_chain
from seedbox2.mix.setrender import Source, layout
from seedbox2.mix.stream import SR

JOB = "set_export"
MIX_FOLDER = "Seedbox Mixes"  # beside the music folder, not in it: that one is watched, and a mix isn't a track to import
RENDER_SHARE, MAKEUP_SHARE = 0.8, 0.15  # of the bar; the encode's pass has the rest
EXPECTED_PASSES = 3  # usual make-up search; the bar waits at the end of its share if it takes more
ETA_AFTER_S = 3.0  # an estimate before this says more about the start-up than the render


class NotReady(ValueError):
    """Something has to happen before this set can be exported. `pairs`: neighbours without a plan."""

    def __init__(self, message: str, pairs: list[dict] | None = None) -> None:
        super().__init__(message)
        self.pairs = pairs or []


def mix_folder() -> Path:
    return paths.folders()["music"].parent / MIX_FOLDER


def spool_root() -> Path:
    return paths.folders()["cache"] / "set-export"


def track_name(track: dict, hash_: str) -> str:
    fallback = next((Path(p).stem for p in track.get("paths", [])), hash_[:8])
    return " - ".join(x for x in (track.get("artist"), track.get("title")) if x) or fallback


def track_gain_db(lib: Any, hash_: str) -> float | None:
    """The gain that puts the track's body at the bus's level, from its stored analysis. What the live engine plays it at."""
    level = lib.analysis(hash_, energy.LOUDNESS_ANALYSER, energy.VERSION)
    if level is None:
        return None
    row = lib.analysis(hash_, energy.ANALYSER, energy.VERSION)
    spans = vibe.spans_of(lib, hash_, row["data"] if row else None)
    return loudness.track_gain_db(level["data"], spans, master_chain.DECK_LUFS)


def _path_of(track: dict, hash_: str) -> Path:
    for path in track.get("paths", []):
        if Path(path).is_file():
            return Path(path)
    raise NotReady(f"the file of {track_name(track, hash_)} can't be found")


def source_of(lib: Any, hash_: str) -> Source:
    track = lib.track(hash_)
    if track is None:
        raise NotReady(f"no track {hash_}")
    path = _path_of(track, hash_)
    gain = track_gain_db(lib, hash_)
    if gain is None:  # no analysis yet: measured from the file, once
        lufs = render.track_lufs(path, SR)
        gain = 0.0 if lufs <= -70 else max(-loudness.TRACK_GAIN_LIMIT_DB, min(loudness.TRACK_GAIN_LIMIT_DB, master_chain.DECK_LUFS - lufs))
    return Source(hash_, track_name(track, hash_), path, gain, offline.cached_stems(hash_) or None)


def missing_pairs(lib: Any, hashes: list[str]) -> list[dict]:
    return [{"from": a, "to": b} for a, b in zip(hashes, hashes[1:]) if lib.plan(a, b) is None]


def check(lib: Any, hashes: list[str] | None) -> None:
    """NotReady if this running order can't be exported yet."""
    if hashes is None or len(hashes) < 2:
        raise NotReady("a set needs at least two tracks to mix")
    missing = missing_pairs(lib, hashes)
    if missing:
        raise NotReady(f"{len(missing)} transition(s) have no plan yet", missing)


def load_plans(lib: Any, hashes: list[str], sources: list[Source]) -> list[MixPlan]:
    stems = {s.hash: bool(s.stems) for s in sources}
    plans = []
    for a, b in zip(hashes, hashes[1:]):
        row = lib.plan(a, b)
        if row is None:
            raise NotReady(f"no plan from {a} to {b}", [{"from": a, "to": b}])
        plans.append(fold_stem_lanes(convert.load(row["plan"], from_track=a, to_track=b), lambda h: stems.get(h, False)))
    return plans


def _left(seconds: float) -> str:
    if seconds < 60:
        return "under a minute left"
    minutes = round(seconds / 60)
    return f"about {minutes} min left" if minutes < 90 else f"about {minutes / 60:.1f} h left"


class _Bar:
    """The job's bar and its estimate, from how fast it has moved in this run: a resumed job starts part way."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.t0 = time.monotonic()
        self.f0: float | None = None

    def __call__(self, fraction: float, doing: str) -> None:
        if self.f0 is None:
            self.f0 = fraction
        elapsed = time.monotonic() - self.t0
        moved = fraction - self.f0
        eta = f" · {_left(elapsed / moved * (1 - fraction))}" if elapsed >= ETA_AFTER_S and moved > 0 else ""
        self.ctx.progress(fraction, doing + eta)


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", name).strip(" .") or "Set"


@register(JOB, pool="heavy")
def set_export_job(payload: dict, ctx: Any) -> dict:
    """Payload `{"set_id", "hashes", "format", "target", "use_anyway"?}`. The result says where the files are and what
    they measure. Fails if a track isn't ready (#99), as `POST /sets/{id}/export` refuses it."""
    from seedbox2.library import ready

    ctx.release_sessions()  # a stems model this worker ran before would sit on the memory budget for a whole render
    lib = ctx.library
    fmt = setexport.FORMATS[payload["format"]]
    hashes = payload["hashes"]
    ready.require(lib, hashes, payload.get("use_anyway") or ())
    check(lib, hashes)
    target_name = payload.get("target") or master_chain.target_name(ctx.settings)
    target_lufs = master_chain.TARGETS[target_name]
    sources = [source_of(lib, h) for h in hashes]
    plans = load_plans(lib, hashes, sources)
    chain = plan_chain(plans, float((lib.track(hashes[-1]) or {}).get("duration") or 0.0))
    where = layout(chain, SR)
    root = spool_root() / str(payload["set_id"])
    spool = setexport.Spool(root / setexport.spool_key(chain, sources, SR))
    for old in root.glob("*") if root.is_dir() else ():  # the plans changed since: that render can't be picked up
        if old != spool.folder:
            setexport.Spool(old).remove()
    setexport.check_disk(root, where.total / SR)
    bar = _Bar(ctx)
    names = [s.name for s in sources]
    try:
        total, files = setexport.render_bus(chain, sources, spool, sr=SR, progress=lambda f, i: bar(
            f * RENDER_SHARE, f"Rendering track {i + 1} of {len(sources)}"))  # fmt: skip
        chunks = max(total / setexport.CHUNK, 1)
        seen = [0]

        def on_pass(index: int) -> None:
            seen[0] += 1
            share = min(seen[0] / (chunks * EXPECTED_PASSES), 1.0)
            bar(RENDER_SHARE + MAKEUP_SHARE * share, f"Measuring the loudness (pass {index + 1})")

        makeup = setexport.find_makeup(spool, files, target_lufs, SR, on_pass)
        folder = Path(payload.get("folder") or mix_folder())
        folder.mkdir(parents=True, exist_ok=True)
        stem = _safe_name(payload.get("name") or (lib.set(payload["set_id"]) or {}).get("name") or "Set")
        out = setexport.free_name(folder, stem, fmt.extension)
        title = payload.get("name") or stem
        written = [0]

        def on_write() -> None:
            written[0] += 1
            bar(RENDER_SHARE + MAKEUP_SHARE + (1 - RENDER_SHARE - MAKEUP_SHARE) * min(written[0] / chunks, 1.0), "Encoding")

        done = setexport.write_mix(spool, files, fmt, out, target_lufs, makeup, sr=SR, title=title,
                                   chapters=setexport.chapters_of(where.track_starts, names, total, SR), progress=on_write)  # fmt: skip
    except Cancelled:
        spool.remove()
        raise
    starts = [s / SR for s in where.track_starts]
    measured = done["measured"]
    tracklist = out.with_suffix(".txt")
    tracklist.write_text(master_chain.tracklist(list(zip(starts, names)), measured, target_lufs), "utf-8")
    cue = out.with_suffix(".cue")
    entries = [(t, (lib.track(s.hash) or {}).get("title") or s.name, (lib.track(s.hash) or {}).get("artist")) for t, s in zip(starts, sources)]
    cue.write_text(setexport.cue_sheet(entries, title, out.name, fmt.cue_type, measured, target_lufs), "utf-8")
    spool.remove()
    return {"path": str(out), "tracklist": str(tracklist), "cue": str(cue), "format": fmt.name, "duration_s": round(total / SR, 3),
            "measured": measured, "master": done["master"], "target": target_name, "tracks": [{"start_s": round(t, 3), "name": n} for t, n in zip(starts, names)]}  # fmt: skip
