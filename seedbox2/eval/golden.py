"""`python -m seedbox2.eval golden`: render the golden corpus, measure it, report it, catch regressions (#87).

    python -m seedbox2.eval golden [--audio DIR]... [--engines seedbox1,seedbox2] [--only TAG_OR_ID]... [--limit N]
                                   [--jobs N] [--out DIR] [--baseline RUN|accepted] [--summary FILE] [--keep-runs N]
    python -m seedbox2.eval golden check [--audio DIR]...        # is the corpus sound, which audio is here?
    python -m seedbox2.eval golden compare BASE[:ENGINE] NEW[:ENGINE] [--out REPORT.html] [--summary FILE]
    python -m seedbox2.eval golden accept RUN                    # this run is the baseline from now on
    python -m seedbox2.eval golden track FILE...                 # [[track]] entries to paste into golden.toml

`golden.toml` holds only hashes and metadata. The audio stays on this machine: a track is found by its library hash
(`seedbox2/library/hashing.py`) in the library's known paths, in `--audio` folders and in `$SEEDBOX2_GOLDEN_AUDIO`
(folders separated by the OS's path separator), so renaming or retagging a file doesn't matter. A pair whose audio
isn't here is listed as not rendered, never counted as passing.

A run is a folder in `$SEEDBOX2_EVAL_RESULTS/golden/<time>-<commit>/`: `run.json` (every metric, the waveforms),
`report.html` (players, plans, waveforms), `renders/` and `plans/`. It never leaves the machine.

To compare engine versions, render each one and compare the runs, or name two engines of one run:

    git worktree add ../old v1 && (cd ../old && python -m seedbox2.eval golden --out /tmp/old)
    python -m seedbox2.eval golden compare /tmp/old <new run>
    python -m seedbox2.eval golden compare <run>:seedbox1 <run>:seedbox2

Exit status: 0 fine, 1 regressions, 2 nothing rendered or a bad corpus.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import multiprocessing
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import mixes
from .mixmetrics import _wheel_steps, measure
from .run import _git_commit, results_dir

FORMAT = "seedbox2-eval-golden/1"
CORPUS = Path(__file__).with_name("golden.toml")
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aif", ".aiff"}
AUDIO_ENV = "SEEDBOX2_GOLDEN_AUDIO"
WAVE_COLUMNS = 600
DURATION_SLACK_S = 3.0  # a file further than this from a track's length isn't decoded to compare hashes
MIN_PAIRS, MAX_PAIRS = 30, 50
GENRES = ("house", "techno", "dnb", "hiphop", "pop", "disco", "halftime")
CASES = ("key-clash", "vocal-heavy")
GAP_BUCKETS = ((0.0, 1.0), (1.0, 3.0), (3.0, 5.0), (5.0, 8.0))  # tempo gap in %; each needs MIN_PER_BUCKET pairs
MIN_PER_TAG, MIN_PER_BUCKET = 3, 3
KEY_CLASH_STEPS = 3  # Camelot wheel steps from which a pair counts as clashing


# --- the corpus ------------------------------------------------------------------------------------------------------


@dataclass
class Track:
    id: str
    hash: str
    artist: str
    title: str
    duration: float
    bpm: float
    key: str
    genre: str
    source: str = ""  # where a royalty-free track can be downloaded; empty for the owner's library


@dataclass
class GoldenPair:
    id: str
    a: str
    b: str
    tags: list[str] = field(default_factory=list)
    technique: str | None = None
    bars: int | None = None


@dataclass
class Corpus:
    tracks: dict[str, Track]
    pairs: list[GoldenPair]
    digest: str = ""


def load_corpus(path: Path = CORPUS) -> Corpus:
    raw = path.read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    tracks = {}
    for t in data.get("track", []):
        if t["id"] in tracks:
            raise ValueError(f"{path}: track {t['id']} is defined twice")
        tracks[t["id"]] = Track(t["id"], t["hash"], t.get("artist", ""), t.get("title", ""), float(t["duration"]),
                                float(t["bpm"]), t.get("key", ""), t.get("genre", ""), t.get("source", ""))  # fmt: skip
    pairs = [GoldenPair(p["id"], p["a"], p["b"], list(p.get("tags", [])), p.get("technique"), p.get("bars"))
             for p in data.get("pair", [])]  # fmt: skip
    return Corpus(tracks, pairs, hashlib.sha1(raw).hexdigest()[:12])


def tempo_gap_pct(a: float, b: float) -> float:
    """How far two tempos are apart in %, counting half and double time as the same tempo (87 and 174 are 0 %)."""
    return min(abs(b * r / a - 1) * 100 for r in (0.5, 1.0, 2.0))


def validate(c: Corpus, *, coverage: bool = True) -> list[str]:
    """What's wrong with the corpus. With `coverage`, also what the ticket asks of it: size, genres, gaps, cases."""
    bad = []
    seen: set[str] = set()
    hashes = Counter(t.hash for t in c.tracks.values())
    bad += [f"hash {h} is used by {n} tracks" for h, n in hashes.items() if n > 1]
    for t in c.tracks.values():
        if len(t.hash) != 40 or any(ch not in "0123456789abcdef" for ch in t.hash):
            bad.append(f"track {t.id}: hash is not a SHA-1")
        if t.bpm <= 0 or t.duration <= 0:
            bad.append(f"track {t.id}: needs a bpm and a duration")
    for p in c.pairs:
        if p.id in seen:
            bad.append(f"pair {p.id} is defined twice")
        seen.add(p.id)
        missing = [x for x in (p.a, p.b) if x not in c.tracks]
        if missing:
            bad.append(f"pair {p.id}: unknown track(s) {', '.join(missing)}")
            continue
        a, b = c.tracks[p.a], c.tracks[p.b]
        if a.hash == b.hash:
            bad.append(f"pair {p.id}: a track into itself")
        if (gap := tempo_gap_pct(a.bpm, b.bpm)) > GAP_BUCKETS[-1][1]:
            bad.append(f"pair {p.id}: tempo gap {gap:.1f}% is over {GAP_BUCKETS[-1][1]:g}%")
        steps = _wheel_steps(a.key, b.key) if a.key and b.key else None
        if "key-clash" in p.tags and (steps is None or steps < KEY_CLASH_STEPS):
            bad.append(f"pair {p.id}: tagged key-clash, but {a.key or '?'} → {b.key or '?'} doesn't clash")
    if not coverage:
        return bad
    if not MIN_PAIRS <= len(c.pairs) <= MAX_PAIRS:
        bad.append(f"{len(c.pairs)} pairs; the corpus should have {MIN_PAIRS}-{MAX_PAIRS}")
    count = Counter(t for p in c.pairs for t in p.tags)
    bad += [f"only {count[t]} pairs tagged {t}" for t in (*GENRES, *CASES) if count[t] < MIN_PER_TAG]
    gaps = [tempo_gap_pct(c.tracks[p.a].bpm, c.tracks[p.b].bpm) for p in c.pairs
            if p.a in c.tracks and p.b in c.tracks]  # fmt: skip
    for lo, hi in GAP_BUCKETS:
        n = sum(lo <= g < hi or (hi == GAP_BUCKETS[-1][1] and g == hi) for g in gaps)
        if n < MIN_PER_BUCKET:
            bad.append(f"only {n} pairs with a tempo gap of {lo:g}-{hi:g}%")
    return bad


def select(c: Corpus, only: Iterable[str] | None, limit: int | None = None) -> list[GoldenPair]:
    """The pairs with any of these ids or tags (all without `only`), the first `limit` of them."""
    wanted = set(only or ())
    pairs = [p for p in c.pairs if not wanted or p.id in wanted or wanted & set(p.tags)]
    return pairs[:limit] if limit else pairs


# --- finding the audio by hash ---------------------------------------------------------------------------------------


def _hash_index_file() -> Path:
    return results_dir() / "golden" / "hash-index.json"


def _load_index(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _library_paths(hashes: set[str]) -> dict[str, Path]:
    """Paths the library knows for these hashes, without opening it for writing or creating it."""
    from seedbox2.paths import folders  # noqa: PLC0415

    db = folders()["settings"] / "library.sqlite3"
    if not db.exists():
        return {}
    out: dict[str, Path] = {}
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
        try:
            for path, h in con.execute("SELECT path, hash FROM track_paths"):
                if h in hashes and Path(path).is_file():
                    out.setdefault(h, Path(path))
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return out


def _probe_duration(path: Path) -> float | None:
    from musicreco.paths import ffmpeg_path  # noqa: PLC0415

    ffmpeg = ffmpeg_path()
    ffprobe = shutil.which("ffprobe") or (str(Path(ffmpeg).with_name("ffprobe")) if ffmpeg else "ffprobe")
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)  # fmt: skip
        return float(r.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None  # can't tell: hash it


def audio_folders(extra: Iterable[Path | str] = ()) -> list[Path]:
    env = os.environ.get(AUDIO_ENV, "")
    return [Path(f).expanduser() for f in [*map(str, extra), *env.split(os.pathsep)] if f.strip()]


def find_audio(tracks: Iterable[Track], folders: Iterable[Path], log: Callable[[str], None] = lambda s: None,
               index_file: Path | None = None) -> dict[str, Path]:  # fmt: skip
    """track id -> a local file with its audio. Hashing means decoding, so only files of about the right length are
    hashed, and hashes are remembered by (path, size, mtime) between runs."""
    from seedbox2.library.hashing import HashError, content_hash  # noqa: PLC0415

    tracks = list(tracks)
    by_hash = {t.hash: t for t in tracks}
    found = {by_hash[h].id: p for h, p in _library_paths(set(by_hash)).items()}
    index_file = index_file or _hash_index_file()
    index = _load_index(index_file)
    durations = sorted(t.duration for t in tracks)
    missing = lambda: [t for t in tracks if t.id not in found]  # noqa: E731
    for folder in folders:
        if not missing():
            break
        if not folder.is_dir():
            log(f"audio folder {folder} isn't there")
            continue
        for root, _dirs, names in os.walk(folder):
            for name in names:
                path = Path(root) / name
                if path.suffix.lower() not in AUDIO_EXTENSIONS:
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                known = index.get(str(path))
                if known and known[0] == st.st_size and known[1] == st.st_mtime_ns:
                    h = known[2]
                else:
                    length = _probe_duration(path)
                    if length is not None and not any(abs(d - length) <= DURATION_SLACK_S for d in durations):
                        continue
                    try:
                        h = content_hash(path).hash
                    except HashError as e:
                        log(f"can't read {path}: {e}")
                        continue
                    index[str(path)] = [st.st_size, st.st_mtime_ns, h]
                if h in by_hash:
                    found.setdefault(by_hash[h].id, path)
    index_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = index_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(index), encoding="utf-8")
    tmp.replace(index_file)
    return found


# --- rendering one pair ----------------------------------------------------------------------------------------------


def peaks(x: np.ndarray, columns: int = WAVE_COLUMNS) -> list[int]:
    """Peak level per column, 0-255: all a report's waveform needs."""
    mono = np.abs(np.asarray(x)).max(axis=0) if np.ndim(x) > 1 else np.abs(np.asarray(x))
    if mono.size == 0:
        return []
    edges = np.linspace(0, mono.size, columns + 1).astype(int)
    out = [float(mono[i:max(j, i + 1)].max()) for i, j in zip(edges[:-1], edges[1:], strict=True) if i < mono.size]
    return [min(255, round(v * 255)) for v in out]


def _encode(mix: np.ndarray, sr: int, target: Path) -> None:
    from scipy.io import wavfile  # noqa: PLC0415

    from musicreco.paths import ffmpeg_path  # noqa: PLC0415

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="seedbox2-golden-") as tmp:
        wav = Path(tmp) / "mix.wav"
        wavfile.write(wav, sr, np.ascontiguousarray(np.clip(mix, -1, 1).T.astype(np.float32)))
        subprocess.run([ffmpeg_path() or "ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(wav), "-b:a", "192k",
                        str(target)], check=True, stdin=subprocess.DEVNULL)  # fmt: skip


def plan_view(plan: dict | None) -> dict | None:
    """What the report draws of a plan: technique, tempo, markers, notes and the lanes that matter."""
    if not plan:
        return None
    decks = {}
    for side in ("a", "b"):
        lanes = (plan.get(side) or {}).get("lanes") or {}
        decks[side] = {k: [[p.get("beat"), p.get("value")] for p in lanes.get(k, [])]
                       for k in ("volume", "eq_low", "eq_mid", "eq_high", "filter")}  # fmt: skip
    return {"technique": plan.get("technique"), "bars": plan.get("bars"),
            "tempo": [t.get("value") for t in plan.get("tempo", [])],
            "markers": [[m.get("beat"), m.get("label")] for m in plan.get("markers", [])],
            "notes": [str(n) for n in plan.get("notes", [])], "decks": decks}  # fmt: skip


@dataclass
class Job:
    pair: GoldenPair
    a: Track
    b: Track
    path_a: Path
    path_b: Path
    engine: str
    run_dir: Path


def render_job(job: Job) -> dict:
    """One pair through one engine. Never raises: a pair that fails shows up as failed in the report."""
    p = job.pair
    try:
        r = mixes.ENGINES[job.engine](mixes.Pair(p.id, job.path_a, job.path_b, job.a.key, job.b.key, p.technique,
                                                 p.bars))  # fmt: skip
    except mixes.EngineUnavailable as e:
        return {"unavailable": str(e)}
    except Exception as e:  # noqa: BLE001 (one bad pair must not stop the night's run)
        return {"error": f"{type(e).__name__}: {e}"}
    try:
        import time  # noqa: PLC0415

        t0 = time.perf_counter()
        metrics = measure(r)
        metrics_s = time.perf_counter() - t0
        mix_rel = f"renders/{p.id}.{job.engine}.mp3"
        _encode(r.mix, r.sr, job.run_dir / mix_rel)
        plan_rel = None
        if r.plan:
            plan_rel = f"plans/{p.id}.{job.engine}.json"
            target = job.run_dir / plan_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(r.plan, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        return {"metrics": metrics, "metrics_s": round(metrics_s, 2), "mix": mix_rel, "plan_file": plan_rel,
                "plan": plan_view(r.plan), "wave": {"mix": peaks(r.mix), "a": peaks(r.decks["A"]),
                                                    "b": peaks(r.decks["B"])}}  # fmt: skip
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def _run_timed(job: Job) -> dict:
    import time  # noqa: PLC0415

    t0 = time.perf_counter()
    res = render_job(job)
    if "metrics" in res:
        res["total_s"] = round(time.perf_counter() - t0, 2)
    return res


def run_corpus(c: Corpus, pairs: list[GoldenPair], engines: list[str], audio: dict[str, Path], run_dir: Path,
               jobs: int = 1, log: Callable[[str], None] = lambda s: None) -> dict:  # fmt: skip
    """Render every pair whose audio is here, with every engine; the run's record."""
    run_dir.mkdir(parents=True, exist_ok=True)
    rows, work = [], []
    for p in pairs:
        a, b = c.tracks[p.a], c.tracks[p.b]
        row = {"id": p.id, "tags": p.tags, "gap_pct": round(tempo_gap_pct(a.bpm, b.bpm), 2),
               "a": _track_meta(a), "b": _track_meta(b), "results": {}}  # fmt: skip
        rows.append(row)
        absent = [t.id for t in (a, b) if t.id not in audio]
        if absent:
            row["missing"] = absent
            continue
        for e in engines:
            work.append((row, e, Job(p, a, b, audio[a.id], audio[b.id], e, run_dir)))
    if jobs > 1 and len(work) > 1:
        with ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("spawn")) as pool:
            futures = [pool.submit(_run_timed, w[2]) for w in work]
            for i, ((row, e, _), fut) in enumerate(zip(work, futures, strict=True), 1):
                row["results"][e] = fut.result()
                log(f"[{i}/{len(work)}] {row['id']} ({e})")
    else:
        for i, (row, e, job) in enumerate(work, 1):
            log(f"[{i}/{len(work)}] {row['id']} ({e})")
            row["results"][e] = _run_timed(job)
    record = {"format": FORMAT, "created": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
              "commit": _git_commit(), "corpus": c.digest, "engines": engines, "pairs": rows}  # fmt: skip
    (run_dir / "run.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return record


def _track_meta(t: Track) -> dict:
    return {"id": t.id, "artist": t.artist, "title": t.title, "bpm": t.bpm, "key": t.key, "genre": t.genre}


# --- metrics and regressions -----------------------------------------------------------------------------------------


def _band(name: str) -> Callable[[dict], float | None]:
    return lambda m: m["masking"].get(name)


@dataclass(frozen=True)
class Metric:
    id: str
    label: str
    get: Callable[[dict], float | None]
    tolerance: float  # how much worse a pair may get before it's flagged; the mean over all pairs gets half of this
    higher_is_better: bool = False


METRICS = [
    Metric("beat_median_ms", "Beat error, median (ms)", lambda m: m["beat"]["median_ms"], 2.0),
    Metric("beat_p95_ms", "Beat error, 95th pct (ms)", lambda m: m["beat"]["p95_ms"], 5.0),
    Metric("bass_s", "Bass overlap (s)", lambda m: m["bass"]["seconds"], 0.5),
    Metric("bass_db_s", "Bass overlap (dB·s)", lambda m: m["bass"]["db_s"], 10.0),
    Metric("vocal_s", "Vocal overlap (s)", lambda m: m["vocals"]["seconds"], 0.5),
    Metric("dip_lu", "Loudness dip (LU)", lambda m: m["loudness"]["dip_lu"], 0.5, higher_is_better=True),
    Metric("bump_lu", "Loudness bump (LU)", lambda m: m["loudness"]["bump_lu"], 0.5),
    Metric("true_peak", "True peak (dBTP)", lambda m: m["loudness"]["true_peak_dbtp"], 0.5),
    Metric("clipped", "Clipped samples", lambda m: m["loudness"]["clipped_samples"], 0.0),
    Metric("mask_sub", "Masking, sub", _band("sub"), 0.05),
    Metric("mask_bass", "Masking, bass", _band("bass"), 0.05),
    Metric("mask_low_mid", "Masking, low mid", _band("low_mid"), 0.05),
    Metric("mask_high_mid", "Masking, high mid", _band("high_mid"), 0.05),
    Metric("mask_high", "Masking, high", _band("high"), 0.05),
    Metric("key_steps", "Key distance (steps)", lambda m: m["key"]["key_steps"], 0.0),
    Metric("stretch_pct", "Stretch used (%)", lambda m: m["key"]["max_stretch_pct"], 0.5),
]
_RANK = {"na": 0, "pass": 0, "warn": 1, "fail": 2}


def _value(metric: Metric, metrics: dict) -> float | None:
    try:
        v = metric.get(metrics)
    except (KeyError, TypeError):
        return None
    return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None


def _worse_by(metric: Metric, base: float, new: float) -> float:
    return base - new if metric.higher_is_better else new - base


def compare(base_rows: list[dict], base_engine: str, new_rows: list[dict], new_engine: str) -> dict:
    """Pair by pair and on the means: what got worse, what got better. A pair that rendered before and doesn't now is
    a regression too, and so is one that dropped out of the run."""
    regressions, improved = [], []
    base_by_id = {r["id"]: r for r in base_rows}
    new_by_id = {r["id"]: r for r in new_rows}
    both = []
    for pid, b_row in base_by_id.items():
        b = b_row["results"].get(base_engine, {})
        if "metrics" not in b:
            continue
        n_row = new_by_id.get(pid)
        n = (n_row or {}).get("results", {}).get(new_engine, {})
        if "metrics" not in n:
            why = n.get("error") or n.get("unavailable") or "not rendered" if n_row else "not in this run"
            regressions.append({"pair": pid, "metric": "render", "label": "Render", "base": "rendered", "new": why})
            continue
        both.append((b["metrics"], n["metrics"]))
        for m in METRICS:
            bv, nv = _value(m, b["metrics"]), _value(m, n["metrics"])
            if bv is None or nv is None:
                continue
            worse = _worse_by(m, bv, nv)
            entry = {"pair": pid, "metric": m.id, "label": m.label, "base": bv, "new": nv}
            if worse > m.tolerance + 1e-9:
                regressions.append(entry)
            elif -worse > m.tolerance + 1e-9:
                improved.append(entry)
        b_checks = {c["id"]: c["status"] for c in b["metrics"].get("checks", [])}
        for c in n["metrics"].get("checks", []):
            old = b_checks.get(c["id"])
            if old is not None and _RANK.get(c["status"], 0) > _RANK.get(old, 0):
                regressions.append({"pair": pid, "metric": f"check_{c['id']}", "label": f"Check: {c['label']}",
                                    "base": old, "new": c["status"]})  # fmt: skip
    means = {}
    for m in METRICS:
        pts = [(bv, nv) for bm, nm in both if (bv := _value(m, bm)) is not None and (nv := _value(m, nm)) is not None]
        if not pts:
            continue
        bv, nv = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        flagged = _worse_by(m, bv, nv) > m.tolerance / 2 + 1e-9
        means[m.id] = {"label": m.label, "base": round(bv, 3), "new": round(nv, 3), "regression": flagged}
        if flagged:
            regressions.append({"pair": "(mean)", "metric": m.id, "label": m.label, "base": round(bv, 3),
                                "new": round(nv, 3)})  # fmt: skip
    return {"regressions": regressions, "improved": improved, "means": means, "compared": len(both)}


def _fmt(v: object) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.2f}" if abs(v) < 100 else f"{v:.0f}"
    return str(v)


def engine_means(rows: list[dict], engine: str) -> dict[str, float]:
    out = {}
    done = [r["results"][engine]["metrics"] for r in rows if "metrics" in r["results"].get(engine, {})]
    for m in METRICS:
        vals = [v for d in done if (v := _value(m, d)) is not None]
        if vals:
            out[m.id] = sum(vals) / len(vals)
    return out


def check_counts(rows: list[dict], engine: str) -> Counter:
    return Counter(c["status"] for r in rows for c in r["results"].get(engine, {}).get("metrics", {}).get("checks", []))


def summary(record: dict, comparison: dict | None = None, baseline: dict | None = None,
            report: Path | None = None, max_lines: int = 25) -> str:  # fmt: skip
    """The run in markdown, for the board."""
    rows = record["pairs"]
    missing = [r["id"] for r in rows if r.get("missing")]
    head = f"**Golden corpus** at `{record.get('commit') or '?'}`, corpus `{record.get('corpus')}`, {record['created']}"
    lines = [head, ""]
    for e in record["engines"]:
        done = sum("metrics" in r["results"].get(e, {}) for r in rows)
        failed = [r["id"] for r in rows if "error" in r["results"].get(e, {})]
        na = sum("unavailable" in r["results"].get(e, {}) for r in rows)
        c = check_counts(rows, e)
        line = f"- `{e}`: {done}/{len(rows)} pairs rendered; checks {c['pass']} pass, {c['warn']} warn, {c['fail']} fail"
        if na:
            line += f"; engine unavailable for {na}"
        if failed:
            line += f"; **{len(failed)} failed**: {', '.join(failed[:10])}" + (" …" if len(failed) > 10 else "")
        lines.append(line)
    if missing:
        lines.append(f"- **{len(missing)} pairs not rendered, audio not found:** {', '.join(missing[:10])}"
                     + (f" and {len(missing) - 10} more" if len(missing) > 10 else ""))  # fmt: skip
    lines.append("")
    primary = record["engines"][0]
    means = engine_means(rows, primary)
    if comparison is not None:
        base_name = f"`{(baseline or {}).get('commit') or '?'}`"
        reg = comparison["regressions"]
        lines.append(f"**{len(reg)} regression{'s' if len(reg) != 1 else ''}** against {base_name} over "
                     f"{comparison['compared']} pairs ({len(comparison['improved'])} metrics improved)."
                     if reg else f"No regressions against {base_name} over {comparison['compared']} pairs "
                                 f"({len(comparison['improved'])} metrics improved).")  # fmt: skip
        if reg:
            lines += ["", "| Pair | Metric | Before | Now |", "|---|---|---|---|"]
            lines += [f"| {x['pair']} | {x['label']} | {_fmt(x['base'])} | {_fmt(x['new'])} |" for x in reg[:max_lines]]
            if len(reg) > max_lines:
                lines += ["", f"…and {len(reg) - max_lines} more in the report."]
    else:
        lines.append("No baseline to compare with.")
    if means:
        lines += ["", f"Means for `{primary}`: " + ", ".join(
            f"{m.label} {_fmt(means[m.id])}" for m in METRICS if m.id in means and m.id in
            ("beat_median_ms", "bass_s", "vocal_s", "dip_lu", "bump_lu", "mask_bass", "stretch_pct"))]  # fmt: skip
    if report:
        lines += ["", f"Report on the machine that ran it: `{report}`"]
    return "\n".join(lines) + "\n"


# --- the report ------------------------------------------------------------------------------------------------------

CSS = """
:root{color-scheme:dark;--bg:#111317;--panel:#1a1d23;--line:#2c3038;--text:#e6e8eb;--dim:#9096a1;--a:#5aa9ff;
--b:#ff8f5a;--mix:#c9ced6;--pass:#46c281;--warn:#e5b13a;--fail:#ef5b5b}
*{box-sizing:border-box}
body{margin:0;padding:28px 32px 80px;background:var(--bg);color:var(--text);
font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0}h3{font-size:12px;margin:0 0 6px;color:var(--dim);
font-weight:600}
.dim{color:var(--dim)}table{border-collapse:collapse;font-variant-numeric:tabular-nums}
td,th{padding:3px 10px;text-align:right;border-bottom:1px solid var(--line);font-weight:400}
td:first-child,th:first-child{text-align:left}th{color:var(--dim);font-size:12px}
section{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px 18px;margin:16px 0}
.chip{display:inline-block;padding:0 7px;border:1px solid var(--line);border-radius:10px;font-size:12px;
color:var(--dim);margin-right:4px}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:18px;margin-top:12px}
audio{width:100%;height:32px;margin:4px 0}
.wave{position:relative;cursor:pointer}svg{display:block;width:100%}
.cursor{position:absolute;top:0;bottom:0;width:1px;background:#fff;left:0;pointer-events:none}
.pass{color:var(--pass)}.warn{color:var(--warn)}.fail{color:var(--fail)}.na{color:var(--dim)}
.bad{color:var(--fail);font-weight:600}.good{color:var(--pass)}
.checks{margin:8px 0;display:flex;flex-wrap:wrap;gap:4px 12px;font-size:12px}
.lanes{display:grid;grid-template-columns:70px 1fr;gap:2px 8px;font-size:11px;color:var(--dim);margin-top:6px}
details{margin-top:8px;font-size:12px}pre{overflow:auto;max-height:320px;background:var(--bg);padding:8px;
border-radius:4px}
.alert{border-color:var(--fail)}.ok{border-color:var(--pass)}
"""

JS = """
document.querySelectorAll('.player').forEach(p=>{
  const a=p.querySelector('audio'),w=p.querySelector('.wave'),c=p.querySelector('.cursor');
  if(!a||!w)return;
  a.addEventListener('timeupdate',()=>{c.style.left=(a.currentTime/a.duration*100)+'%'});
  w.addEventListener('click',e=>{const r=w.getBoundingClientRect();
    a.currentTime=(e.clientX-r.left)/r.width*a.duration;a.play()});
});
"""


def _wave_path(values: list[int], h: float, top: float) -> str:
    """A mirrored peak shape: 0-255 per column, in a viewBox one unit wide per column."""
    if not values:
        return ""
    mid, half = top + h / 2, h / 2 / 255
    up = [f"{i},{mid - v * half:.1f}" for i, v in enumerate(values)]
    down = [f"{i},{mid + v * half:.1f}" for i, v in reversed(list(enumerate(values)))]
    return "M" + "L".join(up + down) + "Z"


def _wave_svg(result: dict) -> str:
    wave = result.get("wave") or {}
    w = max(len(wave.get("mix") or []), 1)
    m = result.get("metrics") or {}
    dur = m.get("duration_s") or 0
    ov = m.get("overlap") or {}
    band = ""
    if dur and ov.get("seconds"):
        x0, x1 = ov["start_s"] / dur * w, ov["end_s"] / dur * w
        band = f'<rect x="{x0:.1f}" y="0" width="{x1 - x0:.1f}" height="110" fill="#fff" opacity=".06"/>'
    return (f'<svg viewBox="0 0 {w} 110" preserveAspectRatio="none" height="110" role="img" '
            f'aria-label="Deck A, deck B and the mix over time">{band}'
            f'<path d="{_wave_path(wave.get("a") or [], 30, 0)}" fill="var(--a)"/>'
            f'<path d="{_wave_path(wave.get("b") or [], 30, 32)}" fill="var(--b)"/>'
            f'<path d="{_wave_path(wave.get("mix") or [], 44, 66)}" fill="var(--mix)"/></svg>')  # fmt: skip


def _lane_svg(points: list[list[float]], lo: float, hi: float, colour: str) -> str:
    span = hi - lo or 1.0
    vals = [p[1] for p in points if p[1] is not None]
    if not vals:
        return ""
    v0, v1 = min(vals), max(vals)
    r = v1 - v0 or 1.0
    pts = " ".join(f"{(p[0] - lo) / span * 400:.1f},{18 - (p[1] - v0) / r * 16:.1f}" for p in points
                   if p[0] is not None and p[1] is not None)  # fmt: skip
    if len(points) == 1:
        pts = f"0,{18 - (vals[0] - v0) / r * 16:.1f} 400,{18 - (vals[0] - v0) / r * 16:.1f}"
    return f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="1.2"/>'


def _plan_html(result: dict, link: str | None) -> str:
    plan = result.get("plan")
    if not plan:
        return ""
    beats = [p[0] for d in plan["decks"].values() for lane in d.values() for p in lane if p[0] is not None]
    lo, hi = (min(beats), max(beats)) if beats else (0.0, 1.0)
    rows = []
    for lane in ("volume", "eq_low", "eq_mid", "eq_high", "filter"):
        a = _lane_svg(plan["decks"]["a"].get(lane) or [], lo, hi, "var(--a)")
        b = _lane_svg(plan["decks"]["b"].get(lane) or [], lo, hi, "var(--b)")
        rows.append(f'<span>{lane}</span><svg viewBox="0 0 400 20" preserveAspectRatio="none" height="20">{a}{b}</svg>')
    markers = ", ".join(f"{html.escape(str(m[1]))} @ {_fmt(m[0])}" for m in plan.get("markers", []))
    notes = "".join(f"<li>{html.escape(n)}</li>" for n in plan.get("notes", []))
    tempo = "→".join(_fmt(t) for t in plan.get("tempo", []))
    head = (f'<div><b>{html.escape(str(plan.get("technique")))}</b>, {plan.get("bars")} bars, {tempo} BPM'
            f'<div class="dim">{markers}</div></div>')  # fmt: skip
    extra = f'<a href="{html.escape(link)}">the plan as JSON</a>' if link else ""
    return (f'{head}<div class="lanes">{"".join(rows)}</div>'
            f'<details><summary>Why, in the planner\'s words</summary><ul>{notes}</ul>{extra}</details>')  # fmt: skip


def _metrics_table(cols: list[dict], flags: dict[str, tuple[str, str]] | None = None) -> str:
    flags = flags or {}
    head = "".join(f"<th>{html.escape(c['label'])}</th>" for c in cols)
    body = []
    for m in METRICS:
        vals = [_value(m, c["result"]["metrics"]) if "metrics" in c["result"] else None for c in cols]
        if all(v is None for v in vals):
            continue
        cls = flags.get(m.id, ("", ""))[0]
        cells = "".join(f'<td class="{cls if i == len(vals) - 1 else ""}">{_fmt(v)}</td>' for i, v in enumerate(vals))
        body.append(f"<tr><td>{html.escape(m.label)}</td>{cells}</tr>")
    return f"<table><tr><th>Metric</th>{head}</tr>{''.join(body)}</table>"


def _href(target: Path, out_dir: Path) -> str:
    """A link from the report to a file of a run: forward slashes on every OS, absolute across Windows drives."""
    try:
        return Path(os.path.relpath(target, out_dir)).as_posix()
    except ValueError:
        return target.resolve().as_uri()


def _column_html(col: dict, out_dir: Path) -> str:
    res = col["result"]
    title = f"<h3>{html.escape(col['label'])}</h3>"
    if "metrics" not in res:
        why = res.get("error") or res.get("unavailable") or "not rendered"
        return f'<div>{title}<div class="{"fail" if "error" in res else "na"}">{html.escape(why)}</div></div>'
    src = html.escape(_href(col["run_dir"] / res["mix"], out_dir))
    plan_link = _href(col["run_dir"] / res["plan_file"], out_dir) if res.get("plan_file") else None
    checks = "".join(f'<span class="{c["status"]}" title="{html.escape(c["detail"])}">'
                     f'{ {"pass": "✓", "warn": "!", "fail": "✗"}.get(c["status"], "·") } {html.escape(c["label"])}'
                     f': {html.escape(c["value"])}</span>' for c in res["metrics"].get("checks", []))  # fmt: skip
    return (f'<div class="player">{title}<audio controls preload="none" src="{src}"></audio>'
            f'<div class="wave">{_wave_svg(res)}<div class="cursor"></div></div>'
            f'<div class="checks">{checks}</div>{_plan_html(res, plan_link)}</div>')  # fmt: skip


def report(columns: list[dict], out: Path, title: str, comparison: dict | None = None) -> Path:
    """Write the HTML report. `columns`: {"label", "record", "engine", "run_dir"}, side by side; the last one is the
    one flagged when there's a comparison. Audio and plans are linked relative to the report, so it needs its runs."""
    out.parent.mkdir(parents=True, exist_ok=True)
    first = columns[-1]["record"]
    rows_by_col = [{r["id"]: r for r in c["record"]["pairs"]} for c in columns]
    ids = [r["id"] for r in first["pairs"]]
    ids += [i for rows in rows_by_col for i in rows if i not in ids]
    ids = list(dict.fromkeys(ids))

    parts = [f"<h1>{html.escape(title)}</h1>"]
    meta = " · ".join(f"{html.escape(c['label'])}: <code>{html.escape(str(c['record'].get('commit')))}</code> "
                      f"{html.escape(c['record'].get('created', ''))}" for c in columns)  # fmt: skip
    parts.append(f'<div class="dim">{meta}</div>')

    per_pair: dict[str, dict[str, tuple[str, str]]] = {}
    reg_by_pair: dict[str, list[dict]] = {}
    if comparison is not None:
        reg = comparison["regressions"]
        for x in reg:
            per_pair.setdefault(x["pair"], {})[x["metric"]] = ("bad", "")
            reg_by_pair.setdefault(x["pair"], []).append(x)
        for x in comparison["improved"]:
            per_pair.setdefault(x["pair"], {}).setdefault(x["metric"], ("good", ""))
        items = "".join(f'<tr><td>{html.escape(x["pair"])}</td><td>{html.escape(x["label"])}</td>'
                        f'<td>{html.escape(_fmt(x["base"]))}</td><td class="bad">{html.escape(_fmt(x["new"]))}</td></tr>'
                        for x in reg)  # fmt: skip
        table = (f"<table><tr><th>Pair</th><th>Metric</th><th>{html.escape(columns[0]['label'])}</th>"
                 f"<th>{html.escape(columns[-1]['label'])}</th></tr>{items}</table>") if reg else ""  # fmt: skip
        parts.append(f'<section class="{"alert" if reg else "ok"}"><h2>{len(reg)} regressions, '
                     f'{len(comparison["improved"])} metrics improved, over {comparison["compared"]} pairs</h2>'
                     f"{table}</section>")  # fmt: skip

    # means and check counts per column
    head = "".join(f"<th>{html.escape(c['label'])}</th>" for c in columns)
    means = [engine_means(c["record"]["pairs"], c["engine"]) for c in columns]
    trs = []
    for m in METRICS:
        if not any(m.id in d for d in means):
            continue
        flagged = bool(comparison and comparison["means"].get(m.id, {}).get("regression"))
        cells = "".join(f'<td class="{"bad" if flagged and i == len(means) - 1 else ""}">{_fmt(d.get(m.id))}</td>'
                        for i, d in enumerate(means))  # fmt: skip
        trs.append(f"<tr><td>{html.escape(m.label)}</td>{cells}</tr>")
    counts = [check_counts(c["record"]["pairs"], c["engine"]) for c in columns]
    trs.append("<tr><td>Checks: pass / warn / fail</td>" + "".join(
        f"<td>{n['pass']} / {n['warn']} / {n['fail']}</td>" for n in counts) + "</tr>")  # fmt: skip
    missing = [r["id"] for r in first["pairs"] if r.get("missing")]
    note = (f'<p class="warn">{len(missing)} pairs not rendered, their audio isn\'t on this machine: '
            f"{html.escape(', '.join(missing))}</p>") if missing else ""  # fmt: skip
    parts.append(f"<section><h2>Means over the pairs rendered</h2><table><tr><th>Metric</th>{head}</tr>{''.join(trs)}"
                 f"</table>{note}</section>")  # fmt: skip

    for pid in ids:
        row = next((rows[pid] for rows in reversed(rows_by_col) if pid in rows), None)
        if row is None:
            continue
        a, b = row["a"], row["b"]
        chips = "".join(f'<span class="chip">{html.escape(t)}</span>' for t in row["tags"])
        cols = [{"label": c["label"], "run_dir": c["run_dir"],
                 "result": rows_by_col[i].get(pid, {}).get("results", {}).get(c["engine"], {})}
                for i, c in enumerate(columns)]  # fmt: skip
        bad = len(reg_by_pair.get(pid, []))
        flag = f' <span class="bad">{bad} regression{"s" if bad != 1 else ""}</span>' if bad else ""
        parts.append(
            f'<section id="{html.escape(pid)}"><h2>{html.escape(pid)}{flag}</h2>'
            f'<div>{html.escape(a["artist"])} – {html.escape(a["title"])} <span class="dim">{a["bpm"]:.1f} BPM '
            f'{html.escape(a["key"])}</span> → {html.escape(b["artist"])} – {html.escape(b["title"])} '
            f'<span class="dim">{b["bpm"]:.1f} BPM {html.escape(b["key"])}, gap {row["gap_pct"]:.1f} %</span></div>'
            f"<div>{chips}</div>"
            + (f'<p class="warn">Not rendered: no audio for {html.escape(", ".join(row["missing"]))}</p>'
               if row.get("missing") else "")
            + f'<div class="cols">{"".join(_column_html(c, out.parent) for c in cols)}</div>'
            + (f"<details{' open' if bad else ''}><summary>Metrics</summary>" + _metrics_table(cols, per_pair.get(pid)) + "</details>"
               if any("metrics" in c["result"] for c in cols) else "")
            + "</section>")  # fmt: skip
    page = (f'<!doctype html><html lang="en"><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f"<style>{CSS}</style><body>{''.join(parts)}<script>{JS}</script></body></html>")  # fmt: skip
    out.write_text(page, encoding="utf-8")
    return out


# --- runs on disk ----------------------------------------------------------------------------------------------------


def golden_dir() -> Path:
    return results_dir() / "golden"


def _accepted_file() -> Path:
    return golden_dir() / "accepted.txt"


def load_run(spec: str | Path) -> tuple[dict, Path]:
    """A run folder or its run.json."""
    path = Path(spec).expanduser()
    if path.is_dir():
        path = path / "run.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("format") != FORMAT:
        raise ValueError(f"{path} isn't a golden run")
    return record, path.parent


def accepted_run() -> Path | None:
    try:
        path = Path(_accepted_file().read_text(encoding="utf-8").strip())
    except OSError:
        return None
    return path if (path / "run.json").is_file() else None


def accept(run_dir: Path) -> None:
    _accepted_file().parent.mkdir(parents=True, exist_ok=True)
    _accepted_file().write_text(str(run_dir.resolve()) + "\n", encoding="utf-8")


def prune(keep: int, also_keep: Iterable[Path] = ()) -> list[Path]:
    """Remove all but the newest `keep` runs and the accepted one. Only folders that hold a run.json are touched."""
    root = golden_dir()
    if not root.is_dir():
        return []
    kept = {p.resolve() for p in also_keep}
    if (acc := accepted_run()) is not None:
        kept.add(acc.resolve())
    runs = sorted((d for d in root.iterdir() if d.is_dir() and (d / "run.json").is_file()), key=lambda d: d.name)
    removed = []
    for d in runs[:-keep] if keep > 0 else []:
        if d.resolve() not in kept:
            shutil.rmtree(d)
            removed.append(d)
    return removed


def _split_spec(spec: str) -> tuple[str, str | None]:
    """RUN or RUN:ENGINE (a path may hold colons: only a known engine name after the last one counts)."""
    head, sep, tail = spec.rpartition(":")
    return (head, tail) if sep and tail in mixes.ENGINES else (spec, None)


def _column(spec: str | Path, engine: str | None = None) -> dict:
    record, run_dir = load_run(spec)
    engine = engine or next((e for e in record["engines"] if any(
        "metrics" in r["results"].get(e, {}) for r in record["pairs"])), record["engines"][0])  # fmt: skip
    if engine not in record["engines"]:
        raise ValueError(f"{spec}: no engine {engine}; it has {', '.join(record['engines'])}")
    label = f"{engine} @ {record.get('commit') or '?'}"
    return {"label": label, "record": record, "engine": engine, "run_dir": run_dir}


def _print_regressions(comparison: dict) -> None:
    reg = comparison["regressions"]
    if reg:
        print(f"{len(reg)} regressions over {comparison['compared']} pairs:", file=sys.stderr)
        for x in reg:
            print(f"  {x['pair']}: {x['label']}: {_fmt(x['base'])} → {_fmt(x['new'])}", file=sys.stderr)
    else:
        print(f"no regressions over {comparison['compared']} pairs")


# --- commands --------------------------------------------------------------------------------------------------------


def cmd_check(a: argparse.Namespace) -> int:
    c = load_corpus(a.corpus)
    problems = validate(c)
    for p in problems:
        print(f"corpus: {p}", file=sys.stderr)
    found = find_audio(c.tracks.values(), audio_folders(a.audio), log=lambda s: print(s, file=sys.stderr))
    pairs_ok = sum(p.a in found and p.b in found for p in c.pairs)
    print(f"{len(c.tracks)} tracks, {len(c.pairs)} pairs; audio here for {len(found)} tracks and {pairs_ok} pairs")
    for t in c.tracks.values():
        if t.id not in found:
            print(f"  no audio: {t.id} {t.artist} – {t.title} ({t.hash})" + (f", from {t.source}" if t.source else ""))
    return 2 if problems else 0


def cmd_run(a: argparse.Namespace) -> int:
    log = lambda s: print(s, file=sys.stderr)  # noqa: E731
    engines = [e.strip() for e in a.engines.split(",") if e.strip()]
    bad = [e for e in engines if e not in mixes.ENGINES]
    if bad:
        print(f"unknown engine(s) {', '.join(bad)}; choose from {', '.join(mixes.ENGINES)}", file=sys.stderr)
        return 2
    try:
        c = load_corpus(a.corpus)
    except (OSError, ValueError, KeyError) as e:
        print(f"bad corpus: {e}", file=sys.stderr)
        return 2
    if problems := validate(c, coverage=a.corpus == CORPUS):
        for p in problems:
            print(f"corpus: {p}", file=sys.stderr)
        return 2
    pairs = select(c, a.only, a.limit)
    needed = [c.tracks[x] for p in pairs for x in (p.a, p.b)]
    audio = find_audio(list({t.id: t for t in needed}.values()), audio_folders(a.audio), log)
    if not any(p.a in audio and p.b in audio for p in pairs):
        print(f"no pair has its audio here ({len(pairs)} asked for): point --audio or ${AUDIO_ENV} at the music, see "
              f"`python -m seedbox2.eval golden check`", file=sys.stderr)  # fmt: skip
        if a.summary:
            a.summary.write_text(f"**Golden corpus**: nothing rendered, none of the {len(pairs)} pairs has its audio "
                                 f"on this machine.\n", encoding="utf-8")  # fmt: skip
        return 2
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = a.out or golden_dir() / f"{stamp}-{_git_commit() or 'nogit'}"
    record = run_corpus(c, pairs, engines, audio, run_dir, a.jobs, log)

    baseline_dir: Path | None = None
    if a.baseline == "accepted":
        baseline_dir = accepted_run()
        if baseline_dir is None:
            accept(run_dir)
            print(f"no accepted baseline yet: this run is it ({run_dir})", file=sys.stderr)
    elif a.baseline:
        baseline_dir = Path(a.baseline)

    columns = [{"label": e, "record": record, "engine": e, "run_dir": run_dir} for e in engines]
    comparison = baseline = None
    if baseline_dir is not None:
        col = _column(baseline_dir, engines[0] if engines[0] in load_run(baseline_dir)[0]["engines"] else None)
        baseline = col["record"]
        comparison = compare(baseline["pairs"], col["engine"], record["pairs"], engines[0])
        columns = [col, *[{**c, "label": f"{c['engine']} @ {record.get('commit') or '?'}"} for c in columns]]
        columns = columns[:2]  # a baseline comparison shows that engine; other engines have their own report
    out = report(columns, run_dir / "report.html", "Seedbox golden corpus", comparison)
    text = summary(record, comparison, baseline, out)
    print(text)
    if a.summary:
        a.summary.write_text(text, encoding="utf-8")
    if comparison is not None:
        _print_regressions(comparison)
    if a.keep_runs:
        prune(a.keep_runs, [run_dir])
    if not any("metrics" in res for r in record["pairs"] for res in r["results"].values()):
        return 2
    return 1 if comparison and comparison["regressions"] else 0


def cmd_compare(a: argparse.Namespace) -> int:
    base_spec, base_engine = _split_spec(a.base)
    new_spec, new_engine = _split_spec(a.new)
    try:
        base, new = _column(base_spec, base_engine), _column(new_spec, new_engine)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    comparison = compare(base["record"]["pairs"], base["engine"], new["record"]["pairs"], new["engine"])
    out = a.out or new["run_dir"] / f"compare-{base['engine']}-{base['record'].get('commit')}.html"
    report([base, new], out, f"{base['label']} vs {new['label']}", comparison)
    text = summary(new["record"], comparison, base["record"], out)
    print(text)
    if a.summary:
        a.summary.write_text(text, encoding="utf-8")
    _print_regressions(comparison)
    return 1 if comparison["regressions"] else 0


def cmd_accept(a: argparse.Namespace) -> int:
    record, run_dir = load_run(a.run)
    accept(run_dir)
    print(f"accepted {run_dir} ({record.get('commit')}) as the baseline")
    return 0


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def cmd_track(a: argparse.Namespace) -> int:
    from musicreco import structure  # noqa: PLC0415
    from musicreco.mixpair import _tag  # noqa: PLC0415
    from seedbox2.library.hashing import content_hash  # noqa: PLC0415

    for f in map(Path, a.files):
        h = content_hash(f)
        an = structure.analyze(f, track_id=f.stem)
        print("[[track]]\n" f'id = "{h.hash[:8]}"\nhash = "{h.hash}"\nartist = {_toml_str(_tag(f, "TPE1"))}\n'
              f'title = {_toml_str(_tag(f, "TIT2") or f.stem)}\nduration = {h.duration:.1f}\nbpm = {an["bpm"]:.2f}\n'
              f'key = {_toml_str(_tag(f, "TKEY"))}\ngenre = ""\n')  # fmt: skip
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m seedbox2.eval golden", description=__doc__.split("\n\n")[0],
                                epilog="Other commands: check, compare, accept, track (each has --help).")  # fmt: skip
    if argv and argv[0] == "check":
        p = argparse.ArgumentParser(prog="python -m seedbox2.eval golden check")
        p.add_argument("--corpus", type=Path, default=CORPUS)
        p.add_argument("--audio", action="append", default=[], metavar="DIR")
        p.set_defaults(fn=cmd_check)
        return p.parse_args(argv[1:])
    if argv and argv[0] == "compare":
        p = argparse.ArgumentParser(prog="python -m seedbox2.eval golden compare")
        p.add_argument("base", metavar="BASE[:ENGINE]", help="a run's folder or run.json")
        p.add_argument("new", metavar="NEW[:ENGINE]")
        p.add_argument("--out", type=Path, help="the report (default: in NEW's folder)")
        p.add_argument("--summary", type=Path, help="also write the markdown summary here")
        p.set_defaults(fn=cmd_compare)
        return p.parse_args(argv[1:])
    if argv and argv[0] == "accept":
        p = argparse.ArgumentParser(prog="python -m seedbox2.eval golden accept")
        p.add_argument("run")
        p.set_defaults(fn=cmd_accept)
        return p.parse_args(argv[1:])
    if argv and argv[0] == "track":
        p = argparse.ArgumentParser(prog="python -m seedbox2.eval golden track")
        p.add_argument("files", nargs="+")
        p.set_defaults(fn=cmd_track)
        return p.parse_args(argv[1:])
    p.add_argument("--corpus", type=Path, default=CORPUS)
    p.add_argument("--audio", action="append", default=[], metavar="DIR", help=f"repeatable; also ${AUDIO_ENV}")
    p.add_argument("--engines", default="seedbox1", help=f"comma-separated, from {', '.join(mixes.ENGINES)}")
    p.add_argument("--only", action="append", metavar="TAG_OR_ID", help="repeatable: only these pairs")
    p.add_argument("--limit", type=int, help="only the first N pairs")
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    p.add_argument("--out", type=Path, help="the run's folder (default: a new one in the results folder)")
    p.add_argument("--baseline", metavar="RUN|accepted", help="compare with this run; `accepted` is the one set by "
                   "`golden accept` (the first such run sets it)")  # fmt: skip
    p.add_argument("--summary", type=Path, help="also write the markdown summary here")
    p.add_argument("--keep-runs", type=int, help="remove all but the newest N runs and the accepted one")
    p.set_defaults(fn=cmd_run)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(sys.argv[1:] if argv is None else argv)
    return a.fn(a)
