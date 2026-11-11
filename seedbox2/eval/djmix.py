"""`python -m seedbox2.eval djmix`: real DJ-mix transitions for the cue model (#55), from the DJ Mix dataset.

    python -m seedbox2.eval djmix build [--root DIR] [--mixes 30] [--genre house ...]   (all four steps below)
    python -m seedbox2.eval djmix select|fetch|analyse|align [--root DIR] ...
    python -m seedbox2.eval cues train ~/.cache/seedbox2/djmix/transitions.jsonl --library ~/.cache/seedbox2/djmix/library.sqlite3

The DJ Mix dataset (`mir-aidj/djmix-dataset`, Kim et al.) lists 5,040 mixes from mixesdb: each mix's audio URL
(SoundCloud, Mixcloud, YouTube), its tracklist with each identified track's YouTube ID, and the mixesdb categories
(DJ, series, genres). It has no cue points: those come from aligning each track's audio with the mix's here.

1. **select**: mixes of the chosen genres with enough identified, consecutive tracks, at most one per DJ, in a fixed
   pseudo-random order (so `--mixes 40` is `--mixes 30` plus ten more).
2. **fetch**: the mix and its tracks with yt-dlp (the same downloader the app uses), into `ROOT/mixes` and
   `ROOT/tracks`. A link that is gone is noted in `ROOT/failed.json` and not tried again.
3. **analyse**: the tracks go into a library of their own (`ROOT/library.sqlite3`, cache in `ROOT/cache`) and get
   Seedbox 2's per-track analyses (beats, energy, sections, stems, vocals): the model learns from what Seedbox sees.
4. **align**: Beat This! beats for the mix and for each track; 32 mel bands averaged per half beat (tempo changes
   between track and mix vanish on a beat grid), z-scored with the track's own statistics, cosine similarity.
   Along each diagonal (the track played straight, one half beat of slack either way) the best run above a
   similarity floor is where the track is heard in the mix. A track is kept when that run is long, clearly better
   than any other place in the mix, and in tracklist order. Two consecutive kept tracks give a transition: B comes in
   where its run starts; A's track time there is its in-point, B's track time its cue-in, and the overlap lasts
   until A's run ends.

Nothing here ships: the audio and what is derived from it stay in ROOT (see "Learnt cue points" in
seedbox2/README.md for the dataset's licence situation).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import zlib
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

DATASET_COMMIT = "e32d179b8529156202566aabaa14bbd344a62d5b"
DATASET_URL = f"https://raw.githubusercontent.com/mir-aidj/djmix-dataset/{DATASET_COMMIT}/dataset/djmix-dataset.json"
# mixesdb categories, most specific first: a mix's genre is the first of these among its categories
GENRES = ("deep house", "tech house", "progressive house", "acid house", "minimal", "techno", "house", "trance",
          "progressive", "electro", "breaks", "drum & bass", "dubstep", "disco", "electronica")  # fmt: skip
DEFAULT_GENRES = ("house", "deep house", "tech house", "techno", "minimal", "progressive house", "progressive")
SOURCES = ("soundcloud", "mixcloud", "youtube")
MIN_PAIRS = 6  # consecutive identified tracks (a transition each) for a mix to be worth downloading
MAX_MIX_S = 3 * 3600

BANDS = 32
FLOOR = 0.25  # similarity above which a half beat counts towards a track's run
MIN_RUN = 64  # half beats (8 bars) of a track heard in the mix, at least
MIN_MEAN = 0.45  # mean similarity over the run
UNIQUE = 1.25  # the run's score over the best one elsewhere in the mix (more than half the track away)
MIN_OVERLAP = 8  # half beats: a shorter overlap is a cut, not a blend
WINDOW_S = 600.0  # a track with a tracklist timestamp is looked for this far around it
# download errors that are rate limits, not dead links: not noted as failed, so the next run tries again
TRANSIENT = ("confirm you’re not a bot", "confirm you're not a bot", "HTTP Error 403", "HTTP Error 429", "timed out")


def default_root() -> Path:
    from seedbox2.paths import folders

    return folders()["cache"] / "djmix"


# --- 1. select --------------------------------------------------------------------------------------------------------


def load_dataset(root: Path) -> list[dict]:
    path = root / "djmix-dataset.json"
    if not path.is_file():
        import requests

        root.mkdir(parents=True, exist_ok=True)
        r = requests.get(DATASET_URL, timeout=120)
        r.raise_for_status()
        path.write_bytes(r.content)
    return json.loads(path.read_text())


def categories(mix: dict) -> list[str]:
    return [str(t.get("key", "")).removeprefix("Category:").strip() for t in mix.get("tags") or []]


def genre_of(mix: dict) -> str:
    cats = {c.lower() for c in categories(mix)}
    return next((g for g in GENRES if g in cats), "")


def timestamp_s(title: str) -> float | None:
    """`[05]` (minutes), `[06:15]` (minutes:seconds) or `[1:02:30]` at the start of a tracklist line."""
    m = re.match(r"^\[(\d+(?::\d+){0,2})\]", title or "")
    if not m:
        return None
    parts = [int(p) for p in m.group(1).split(":")]
    if len(parts) == 1:
        return parts[0] * 60.0
    if len(parts) == 2:
        return parts[0] * 60.0 + parts[1]
    return parts[0] * 3600.0 + parts[1] * 60.0 + parts[2]


def pairs(mix: dict) -> int:
    ids = [t.get("id") for t in mix.get("tracklist") or []]
    return sum(1 for a, b in zip(ids, ids[1:]) if a and b and a != b)


def select(dataset: list[dict], n: int, genres: Iterable[str] = DEFAULT_GENRES, min_pairs: int = MIN_PAIRS) -> list[dict]:
    """Up to `n` mixes: the genres', with `min_pairs` transitions, one per DJ (their first category), in a fixed order."""
    wanted = {g.lower() for g in genres}
    out, djs = [], set()
    for mix in sorted(dataset, key=lambda m: zlib.crc32(m["id"].encode())):
        if mix.get("audio_source") not in SOURCES or genre_of(mix) not in wanted or pairs(mix) < min_pairs:
            continue
        dj = (categories(mix) or [mix["id"]])[0].lower()
        if dj in djs:
            continue
        djs.add(dj)
        out.append(mix)
        if len(out) >= n:
            break
    return out


# --- 2. fetch ---------------------------------------------------------------------------------------------------------


def _ytdlp(url: str, out_base: Path, audio_format: str | None) -> Path:
    """Download `url`'s best audio to `out_base`.<ext>, returning the file. Raises on failure. The file appears only
    once it is complete (downloaded and converted in a staging folder next to it), so another process can import
    what's there while this one downloads."""
    stage = out_base.parent / ".partial" / out_base.name
    stage.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-playlist", "-f", "bestaudio/best",
           "-o", str(stage / "audio.%(ext)s")]  # fmt: skip
    if audio_format:
        cmd += ["-x", "--audio-format", audio_format]
    from musicreco.paths import ffmpeg_path

    if ffmpeg := ffmpeg_path():
        cmd += ["--ffmpeg-location", str(ffmpeg)]
    try:
        p = subprocess.run([*cmd, "--", url], capture_output=True, text=True, timeout=1800)
        done = _existing(stage / "audio")
        if p.returncode != 0 or done is None:
            raise RuntimeError(((p.stderr or p.stdout or "").strip().splitlines() or ["yt-dlp failed"])[-1][:300])
        target = Path(f"{out_base}{done.suffix}")
        os.replace(done, target)
        return target
    finally:
        import shutil

        shutil.rmtree(stage, ignore_errors=True)


def _existing(base: Path) -> Path | None:
    """The downloaded file (a mix may come as .webm: it is only decoded by ffmpeg, never imported)."""
    from seedbox2.library.tags import is_audio

    if not base.parent.is_dir():
        return None
    return next((p for p in sorted(base.parent.glob(base.name + ".*")) if is_audio(p) or p.suffix == ".webm"), None)


class Failed:
    """ROOT/failed.json: downloads that failed, so a rerun doesn't try them again."""

    def __init__(self, root: Path) -> None:
        self.path = root / "failed.json"
        self.data: dict[str, str] = json.loads(self.path.read_text()) if self.path.is_file() else {}
        self._lock = threading.Lock()

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def add(self, key: str, why: str) -> None:
        with self._lock:
            self.data[key] = why
            self.path.write_text(json.dumps(self.data, indent=1) + "\n")


def mix_file(root: Path, mix_id: str) -> Path | None:
    return _existing(root / "mixes" / mix_id)


def track_file(root: Path, yid: str) -> Path | None:
    return _existing(root / "tracks" / yid)


def fetch(root: Path, mixes: list[dict], workers: int = 6, parallel_mixes: int = 3,
          say: Callable[[str], None] = print) -> None:  # fmt: skip
    """Download the mixes (`parallel_mixes` at once: they stream slowly) and their tracks."""
    failed = Failed(root)

    def get(base: Path, url: str, audio_format: str | None) -> None:
        if _existing(base) is None and base.name not in failed:
            try:
                _ytdlp(url, base, audio_format)
            except Exception as e:
                if not any(m in str(e) for m in TRANSIENT):  # rate limits: the next run tries again
                    failed.add(base.name, str(e))

    def one_mix(i_mix: tuple[int, dict]) -> None:
        i, mix = i_mix
        mid = mix["id"]
        t0 = time.monotonic()
        get(root / "mixes" / mid, mix["audio_url"], None)
        if mix_file(root, mid) is None:
            say(f"[{i}/{len(mixes)}] {mid}: mix not downloaded: {failed.data.get(mid)}")
            return
        ids = list(dict.fromkeys(t["id"] for t in mix["tracklist"] if t.get("id")))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda y: get(root / "tracks" / y, f"https://www.youtube.com/watch?v={y}", "opus"), ids))
        have = sum(track_file(root, y) is not None for y in ids)
        say(f"[{i}/{len(mixes)}] {mid}: mix and {have}/{len(ids)} tracks ({time.monotonic() - t0:.0f} s)")

    with ThreadPoolExecutor(max_workers=parallel_mixes) as pool:
        list(pool.map(one_mix, enumerate(mixes, 1)))


# --- 3. analyse -------------------------------------------------------------------------------------------------------


def use_root(root: Path) -> None:
    """Point Seedbox 2's cache and settings at ROOT, sharing the downloaded models, so nothing lands in the app's."""
    from seedbox2.models import store

    shared_models = store.models_dir()
    cache = root / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    if not (cache / "models").exists() and shared_models.is_dir():
        (cache / "models").symlink_to(shared_models, target_is_directory=True)
    os.environ["SEEDBOX_CACHE_DIR"] = str(cache)
    os.environ["SEEDBOX_SUPPORT_DIR"] = str(root)
    settings = root / "settings.json"
    if not settings.is_file():  # keep the stems cache small: the vocals analysis stays when its stems go
        settings.write_text(json.dumps({"stems": {"limit_gb": 4}}) + "\n")


def open_library(root: Path) -> Any:
    from seedbox2.library.repo import Library

    return Library.open(root / "library.sqlite3")


def analyse(root: Path, mixes: list[dict], heavy_workers: int = 1, say: Callable[[str], None] = print) -> dict[str, str]:
    """Import the mixes' downloaded tracks into ROOT's library, run every per-track analysis and wait for them.
    Returns {youtube id: library hash}."""
    from seedbox2.analysis import energy, key, sections, stems, vibe, vocals
    from seedbox2.jobs.supervisor import Supervisor
    from seedbox2.library.importer import Importer, queue_analysis

    use_root(root)
    lib = open_library(root)
    files = {y: f for m in mixes for t in m["tracklist"] if (y := t.get("id")) and (f := track_file(root, y))}
    report = Importer(lib).import_paths(files.values())
    if report.errors:
        say(f"{len(report.errors)} tracks not imported: {report.errors[:3]}")
    hashes = hash_index(lib, files)
    todo = [h for h in hashes.values() if lib.analysis(h, "vocals") is None or lib.analysis(h, "sections") is None]
    if not todo:
        return hashes
    jobs = Supervisor(lib, None, heavy_workers=heavy_workers)
    jobs.add_listener(lambda row: stems.on_job_finished(jobs, lib, None, row))
    for mod in (key, energy, vibe, vocals, sections):
        jobs.add_listener(lambda row, mod=mod: mod.on_job_finished(jobs, lib, row))
    jobs.start()
    try:
        fresh = [h for h in todo if lib.analysis(h, "beats") is None]
        queue_analysis(jobs, fresh)  # everything, for tracks never analysed
        for h in todo:  # the rest only need what's missing: a cached stems job queues vocals and sections again
            if h not in fresh:
                jobs.submit("stems", {"hash": h}, priority="background")
        t0 = time.monotonic()
        last = 0.0
        while active := [j for j in jobs.jobs() if j["state"] in ("queued", "running")]:
            if time.monotonic() - last > 60:
                last = time.monotonic()
                left = sum(j["kind"] == "stems" for j in active)
                say(f"analysing {len(todo)} tracks: {left} stems left, {time.monotonic() - t0:.0f} s")
            time.sleep(2)
    finally:
        jobs.stop()
    failed = [j for j in jobs.jobs() if j["state"] == "failed"]
    if failed:
        say(f"{len(failed)} analysis jobs failed, e.g. {failed[0]['kind']}: {failed[0].get('error')}")
    return hashes


def hash_index(lib: Any, files: dict[str, Path]) -> dict[str, str]:
    out = {}
    for yid, f in files.items():
        row = lib.track_for_path(os.path.abspath(f))
        if row is not None:
            out[yid] = row["hash"]
    return out


# --- 4. align ---------------------------------------------------------------------------------------------------------


@dataclass
class Beats:
    """Half-beat boundaries (seconds) and the mel bands averaged over each half beat."""

    t: np.ndarray  # n + 1
    f: np.ndarray  # n x BANDS


class FeatureCache:
    def __init__(self, root: Path) -> None:
        self.dir = root / "features"
        self._session: Any = None

    def session(self) -> Any:
        if self._session is None:
            from seedbox2.models import runtime

            self._session = runtime.load_session("beat_this", pool="heavy")
        return self._session

    def get(self, key: str, path: Path) -> Beats | None:
        c = self.dir / f"{key}.npz"
        if c.is_file():
            z = np.load(c)
            return Beats(z["t"], z["f"]) if z["t"].size > 1 else None
        b = half_beats(path, self.session())
        self.dir.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(c, t=b.t if b else np.zeros(0), f=b.f if b else np.zeros((0, BANDS)))
        return b


def half_beats(path: Path, session: Any) -> Beats | None:
    """Beat This! beats on one constant-or-piecewise grid (no octave change: track and mix get the same treatment)."""
    from seedbox2.analysis import beats, beatthis
    from seedbox2.analysis.grid import fit

    x = beats.decode(path)
    if x.size < beatthis.SAMPLE_RATE * 10:
        return None
    act = beatthis.run(x, beatthis.session_runner(session))
    bt = np.asarray(act.beats, dtype=np.float64)
    f = fit(bt, [beats.acf_period(act.beat, act.fps)]) if bt.size >= 16 else None
    if f is None:
        return None
    g = f.grid
    k = np.arange(np.ceil(g.beat_at(bt[0])), np.floor(g.beat_at(bt[-1])) + 0.5, 0.5)
    t = np.asarray(g.time_at(k), dtype=np.float64)
    mel = beatthis.log_mel(x)
    mel = mel.reshape(mel.shape[0], BANDS, -1).mean(axis=2)
    fr = np.clip(np.round(t * beatthis.FPS).astype(int), 0, len(mel))
    cs = np.vstack([np.zeros((1, BANDS)), np.cumsum(mel, axis=0, dtype=np.float64)])
    hb = (cs[fr[1:]] - cs[fr[:-1]]) / np.maximum(fr[1:] - fr[:-1], 1)[:, None]
    return Beats(t, hb.astype(np.float32))


@dataclass
class Match:
    offset: int  # mix half beat = track half beat + offset
    t0: int  # the run, in track half beats (inclusive)
    t1: int
    score: float
    mean: float
    unique: float  # score over the best run elsewhere

    @property
    def m0(self) -> int:
        return self.t0 + self.offset

    @property
    def m1(self) -> int:
        return self.t1 + self.offset


def _unit(x: np.ndarray, mu: np.ndarray, sd: np.ndarray) -> np.ndarray:
    y = (x - mu) / sd
    return y / np.maximum(np.linalg.norm(y, axis=1, keepdims=True), 1e-6)


def match(track: np.ndarray, mix: np.ndarray, lo: int = 0, hi: int | None = None, floor: float = FLOOR,
          slack: int = 1) -> Match | None:  # fmt: skip
    """Where `track` (half beats x bands) plays in `mix`, its start searched in mix half beats [lo, hi)."""
    n, m = len(track), len(mix)
    if n < MIN_RUN or m < MIN_RUN:
        return None
    hi = m if hi is None else min(hi, m)
    mu, sd = track.mean(axis=0), track.std(axis=0) + 1e-3
    a, b = _unit(track, mu, sd).astype(np.float32), _unit(mix, mu, sd).astype(np.float32)
    offsets = np.arange(max(lo - n, -n), hi)  # every diagonal that reaches into the window
    s = a @ b.T  # n x m
    pad = n + slack + 1
    sp = np.pad(s, ((0, 0), (pad, pad)), constant_values=-1.0)
    rows = np.arange(n)[:, None]
    cols = rows + offsets[None, :] + pad
    d = sp[rows, cols]
    for k in range(1, slack + 1):
        d = np.maximum(d, np.maximum(sp[rows, cols - k], sp[rows, cols + k]))
    x = d - floor
    # best run per diagonal (Kadane, vectorised over the diagonals)
    cur = np.zeros(len(offsets))
    start = np.zeros(len(offsets), dtype=int)
    best = np.full(len(offsets), -np.inf)
    b0 = np.zeros(len(offsets), dtype=int)
    b1 = np.zeros(len(offsets), dtype=int)
    for t in range(n):
        restart = cur <= 0
        cur = np.where(restart, x[t], cur + x[t])
        start = np.where(restart, t, start)
        up = cur > best
        best = np.where(up, cur, best)
        b0 = np.where(up, start, b0)
        b1 = np.where(up, t, b1)
    starts = b0 + offsets
    ok = (starts >= lo) & (starts < hi)
    if not ok.any():
        return None
    j = int(np.argmax(np.where(ok, best, -np.inf)))
    far = np.abs(offsets - offsets[j]) > max(n // 2, MIN_RUN)  # elsewhere in the mix, not a few bars off
    other = float(best[far].max()) if far.any() else 0.0
    return Match(offset=int(offsets[j]), t0=int(b0[j]), t1=int(b1[j]), score=float(best[j]),
                 mean=float(d[b0[j] : b1[j] + 1, j].mean()), unique=float(best[j] / max(other, 1e-6)))  # fmt: skip


def accepted(mt: Match | None) -> bool:
    return mt is not None and mt.t1 - mt.t0 + 1 >= MIN_RUN and mt.mean >= MIN_MEAN and mt.unique >= UNIQUE


def mix_transitions(mix: dict, mixb: Beats, tracks: dict[str, Beats], hashes: dict[str, str],
                    say: Callable[[str], None] = lambda s: None) -> tuple[list[dict], list[dict]]:  # fmt: skip
    """(transitions, per-track matches) of one mix."""
    entries = mix["tracklist"]
    matches: list[Match | None] = []
    rows = []
    prev_m0 = -1
    for i, e in enumerate(entries):
        yid = e.get("id")
        tb = tracks.get(yid) if yid else None
        mt = None
        if tb is not None:
            ts = timestamp_s(e.get("title", ""))
            lo, hi = 0, len(mixb.f)
            if ts is not None:
                lo = int(np.searchsorted(mixb.t, ts - WINDOW_S))
                hi = int(np.searchsorted(mixb.t, ts + WINDOW_S))
            mt = match(tb.f, mixb.f, lo, hi)
        ok = accepted(mt) and mt.m0 > prev_m0
        if ok:
            prev_m0 = mt.m0
        matches.append(mt if ok else None)
        rows.append({"i": i, "id": yid, "ok": ok, **({} if mt is None else {
            "mix_in_s": round(float(mixb.t[max(0, min(mt.m0, len(mixb.t) - 1))]), 2), "offset": mt.offset,
            "t0": mt.t0, "t1": mt.t1, "mean": round(mt.mean, 3), "unique": round(mt.unique, 2)})})  # fmt: skip
    out = []
    for i in range(1, len(entries)):
        ma, mb = matches[i - 1], matches[i]
        ya, yb = entries[i - 1].get("id"), entries[i].get("id")
        if ma is None or mb is None or ya not in hashes or yb not in hashes or ya == yb:
            continue
        ta, tb = tracks[ya], tracks[yb]
        start = mb.m0  # mix half beat where B comes in
        end = min(ma.m1, mb.m1) + 1
        if end - start < MIN_OVERLAP or start <= ma.m0:
            continue
        ia = start - ma.offset  # A's half beat there
        ie = min(end - ma.offset, len(ta.t) - 1)
        if not 0 <= ia < len(ta.t) - 1:
            continue
        out.append({
            "mix": mix["id"], "genre": genre_of(mix), "a": {"hash": hashes[ya]}, "b": {"hash": hashes[yb]},
            "a_in_s": round(float(ta.t[ia]), 3), "b_cue_s": round(float(tb.t[mb.t0]), 3),
            "length_s": round(float(ta.t[ie] - ta.t[ia]), 3),
            "align": {"a": ya, "b": yb, "mix_in_s": round(float(mixb.t[start]), 2),
                      "mean": [round(ma.mean, 3), round(mb.mean, 3)]},
        })  # fmt: skip
    return out, rows


def align(root: Path, mixes: list[dict], hashes: dict[str, str], say: Callable[[str], None] = print) -> list[dict]:
    feats = FeatureCache(root)
    out_dir = root / "alignments"
    out_dir.mkdir(parents=True, exist_ok=True)
    allt: list[dict] = []
    for i, mix in enumerate(mixes, 1):
        mid = mix["id"]
        done = out_dir / f"{mid}.json"
        if done.is_file():
            allt += json.loads(done.read_text())["transitions"]
            continue
        mf = mix_file(root, mid)
        if mf is None:
            continue
        t0 = time.monotonic()
        mixb = feats.get(mid, mf)
        tracks = {}
        for e in mix["tracklist"]:
            if (y := e.get("id")) and (f := track_file(root, y)) and y not in tracks:
                if (b := feats.get(y, f)) is not None:
                    tracks[y] = b
        ts, rows = mix_transitions(mix, mixb, tracks, hashes) if mixb is not None else ([], [])
        done.write_text(json.dumps({"mix": mid, "tracks": rows, "transitions": ts}, indent=1) + "\n")
        found = sum(r["ok"] for r in rows)
        say(f"[{i}/{len(mixes)}] {mid}: {found}/{len(rows)} tracks found, {len(ts)} transitions "
            f"({time.monotonic() - t0:.0f} s)")  # fmt: skip
        allt += ts
    return allt


# --- CLI --------------------------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m seedbox2.eval djmix", description=__doc__.split("\n\n")[0])
    ap.add_argument("step", choices=("build", "select", "fetch", "analyse", "align"))
    ap.add_argument("--root", type=Path, default=None, help="where everything goes (default: <cache>/djmix)")
    ap.add_argument("--mixes", type=int, default=30, help="how many mixes (default 30)")
    ap.add_argument("--genre", action="append", help=f"mixesdb genre (repeatable; default: {', '.join(DEFAULT_GENRES)})")
    ap.add_argument("--heavy-workers", type=int, default=1, help="stem separations at once (default 1)")
    ap.add_argument("--out", type=Path, help="transitions file (default ROOT/transitions.jsonl)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    root = (args.root or default_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    mixes = select(load_dataset(root), args.mixes, args.genre or DEFAULT_GENRES)
    print(f"{len(mixes)} mixes in {root}")
    if args.step == "select":
        for m in mixes:
            print(f"{m['id']}  {genre_of(m):18} {pairs(m):3} pairs  {m['title']}")
        return 0
    if args.step in ("build", "fetch"):
        fetch(root, mixes)
    if args.step in ("build", "analyse"):
        hashes = analyse(root, mixes, args.heavy_workers)
    else:
        use_root(root)
        lib = open_library(root)
        files = {y: f for m in mixes for t in m["tracklist"] if (y := t.get("id")) and (f := track_file(root, y))}
        hashes = hash_index(lib, files)
    if args.step in ("build", "align"):
        ts = align(root, mixes, hashes)
        out = args.out or root / "transitions.jsonl"
        out.write_text("".join(json.dumps(t) + "\n" for t in ts))
        print(f"wrote {len(ts)} transitions from {len({t['mix'] for t in ts})} mixes to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
