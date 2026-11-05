"""Vocal activity, phrases and acapella/instrumental windows from the vocal stem (#47).

Seedbox 1 guesses vocals from harmonic energy in the voice band of the mix (`musicreco.structure._vocal_score`), so
synth leads read as vocals and chops are missed. Here the question is asked of HTDemucs' vocal stem, where the only
things left to tell apart are a voice and what bled through:

1. **Frames** (`frame_features`, 23 ms): the stem's level against the mix, its spectral flatness and its harmonicity
   in the voice band. `likelihood` weighs them (`WEIGHTS`, a logistic regression fitted on Jamendo's 16-track
   validation split). The level does nearly all the work: refitted on it alone, the validation split's frame F is
   the same 0.932, so flatness and harmonicity earn no measured gain there. Harmonicity still tells rap from singing
   in `classify`.
2. **Activity** (`frame_activity`, `beat_activity`): a median filter and two thresholds, so a note hovering at the
   threshold doesn't flicker, then the share of each beat that is sung. A beat turns on at `BEAT_ON` and off below
   `BEAT_OFF`.
3. **Phrases** (`phrases`): active beats merged across gaps of up to `PHRASE_GAP_BEATS`, each with a `kind`
   (`classify`): `lead`, `rap`, `chop` or `backing`.
4. **Windows** (`windows`): `acapella` (vocals with next to nothing under them) and `instrumental` (music without
   vocals), for mashup moments.

The result (`analyse`, stored as the track's `vocals` analysis), beats numbered as in `seedbox2.analysis.grid`:

    {"version", "stems_model", "segments" (the grid), "first_beat", "activity": [0-1 per beat],
     "active": ["0"/"1" per beat, as one string],
     "phrases": [{start_beat, end_beat, start_s, end_s, kind, level_db, harmonicity}],
     "acapella": [{start_beat, end_beat, start_s, end_s}], "instrumental": [...], "duration_s"}

`plan_input` puts this into a Seedbox 1 analysis in place of `vocal_per_bar` and `has_vocals`, which is what
`musicreco.transitions` reads for its clash checks. A track without stems keeps the heuristic.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
from scipy import fft as sfft
from scipy.ndimage import median_filter

from seedbox2.analysis.grid import Grid
from seedbox2.analysis.waveform import SAMPLE_RATE, decode_chunks, stem_files
from seedbox2.jobs import register

log = logging.getLogger(__name__)

ANALYSER = "vocals"
VERSION = "1"
VOCAL_STEM = "vocals"

NFFT = 2048
HOP = 512
FRAME_S = HOP / SAMPLE_RATE
BAND_HZ = (150.0, 5000.0)  # flatness and harmonicity look at the voice, not at hats or sub bass
PITCH_HZ = (80.0, 800.0)
EPS = 1e-10
SILENCE_DB = -60.0  # dBFS: below this a stem frame is nothing at all

# likelihood: bias + rel_db * dB of the stem against the mix + flatness * [0, 1] + harmonicity * [0, 1]
WEIGHTS = {"bias": 3.502, "rel_db": 0.1522, "flatness": 1.041, "harmonicity": 0.303}  # fitted, see the module docstring
SMOOTH_S = 0.25
FRAME_ON = 0.6
FRAME_OFF = 0.4
BEAT_ON = 0.5
BEAT_OFF = 0.25

PHRASE_GAP_BEATS = 2
CHOP_MAX_BEATS = 2  # a phrase this short is a one-shot
CHOP_BURST_S = 0.2  # ... as is a longer one made of bursts shorter than this, with more gap than voice
CHOP_MAX_DUTY = 0.5
RAP_MAX_HARMONICITY = 0.45  # the median of a phrase's active frames; singing holds notes, speech mostly doesn't
BACKING_BELOW_LEAD_DB = 8.0  # against the track's loudest phrases
BACKING_MIN_SIDE = 0.35  # or wide: side / (mid + side) energy, for phrases at most BACKING_NEAR_LEAD_DB below lead
BACKING_NEAR_LEAD_DB = 3.0

ACAPELLA_BELOW_VOCAL_DB = 12.0  # what's under the vocal is at least this far below it
ACAPELLA_MIN_BEATS = 4
INSTRUMENTAL_MIN_BEATS = 8
WINDOW_GAP_BEATS = 1
MUSIC_FLOOR_DB = 30.0  # below the track's loud level there's no music to speak of

ON_BAR = 0.5  # as musicreco.transitions.VOCAL_ON and structure.py: a bar at/above this counts as sung
EDGE_BARS = 8  # a 0-bar edge reports on the first/last 8 bars (structure.py)
EDGE_SHARE = 0.25


def _db(power: np.ndarray) -> np.ndarray:
    return 10.0 * np.log10(power + EPS)


def _frames(x: np.ndarray) -> np.ndarray:
    n = 1 + max(0, x.size - NFFT) // HOP
    if x.size < NFFT:
        x = np.pad(x, (0, NFFT - x.size))
    return np.lib.stride_tricks.sliding_window_view(x, NFFT)[::HOP][:n]


def _power(x: np.ndarray) -> np.ndarray:
    """Mean square of each frame of `x`, without keeping the frames: a running sum over HOP blocks is enough."""
    c = np.concatenate(([0.0], np.cumsum(x.astype(np.float64) ** 2)))
    n = 1 + max(0, x.size - NFFT) // HOP
    starts = np.arange(n) * HOP
    return (c[np.minimum(starts + NFFT, x.size)] - c[starts]) / NFFT


def frame_features(vocal: np.ndarray, rest: np.ndarray, *, chunk_frames: int = 2048) -> dict[str, np.ndarray]:
    """Per frame: `vocal_db`, `rest_db` and `mix_db` (dBFS), `flatness`, `harmonicity`, `side`. `vocal` is (2, n) or
    (n,); `rest` is the sum of the other stems, mono."""
    stereo = vocal.ndim == 2
    mono = vocal.mean(axis=0) if stereo else vocal
    n = min(mono.size, rest.size)
    mono, rest = mono[:n], rest[:n]
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(NFFT) / NFFT)).astype(np.float32)
    freqs = np.fft.rfftfreq(NFFT, 1 / SAMPLE_RATE)
    band = (freqs >= BAND_HZ[0]) & (freqs <= BAND_HZ[1])
    lag0 = int(SAMPLE_RATE / PITCH_HZ[1])
    lag1 = int(SAMPLE_RATE / PITCH_HZ[0])
    win_ac = sfft.irfft(np.abs(sfft.rfft(window, 2 * NFFT)) ** 2)[: lag1 + 1]
    win_ac = win_ac / win_ac[0]
    padded_freqs = np.fft.rfftfreq(2 * NFFT, 1 / SAMPLE_RATE)
    padded_band = (padded_freqs >= BAND_HZ[0]) & (padded_freqs <= BAND_HZ[1])

    frames = _frames(mono)
    flat, harm = np.zeros(len(frames), np.float32), np.zeros(len(frames), np.float32)
    for i in range(0, len(frames), chunk_frames):  # chunks: a whole track's spectra at once would be gigabytes
        spec = sfft.rfft(frames[i : i + chunk_frames] * window, 2 * NFFT, axis=1, workers=1)
        power = spec.real**2 + spec.imag**2
        in_band = power[:, ::2][:, band] + EPS  # every second bin: the padded FFT's bins on the plain FFT's grid
        flat[i : i + chunk_frames] = np.exp(np.mean(np.log(in_band), axis=1)) / np.mean(in_band, axis=1)
        ac = sfft.irfft(power * padded_band, axis=1, workers=1)[:, : lag1 + 1]
        ac = ac / np.maximum(ac[:, :1], EPS) / np.maximum(win_ac, 0.05)  # the window's own decay doesn't count
        harm[i : i + chunk_frames] = np.clip(ac[:, lag0:].max(axis=1), 0.0, 1.0)

    vocal_p = _power(mono)
    side = np.zeros(vocal_p.size)
    if stereo:
        side_p = _power((vocal[0, :n] - vocal[1, :n]) * 0.5)
        side = side_p / (side_p + vocal_p + EPS)
    return {"vocal_db": _db(vocal_p), "rest_db": _db(_power(rest)), "mix_db": _db(_power(mono + rest)),
            "flatness": flat, "harmonicity": harm, "side": side}  # fmt: skip


def likelihood(f: dict[str, np.ndarray], weights: dict[str, float] = WEIGHTS) -> np.ndarray:
    """P(voice) per frame. A silent stem is 0 whatever its (meaningless) flatness and harmonicity."""
    rel = np.clip(f["vocal_db"] - f["mix_db"], -40.0, 0.0)
    z = weights["bias"] + weights["rel_db"] * rel + weights["flatness"] * f["flatness"]
    z = z + weights["harmonicity"] * f["harmonicity"]
    p = 1.0 / (1.0 + np.exp(-z))
    return np.where(f["vocal_db"] < SILENCE_DB, 0.0, p)


def hysteresis(x: np.ndarray, on: float, off: float) -> np.ndarray:
    """True from where `x` reaches `on` until it falls below `off`."""
    out = np.zeros(x.size, bool)
    state = False
    for i, v in enumerate(x):
        state = v >= off if state else v >= on
        out[i] = state
    return out


def frame_activity(p: np.ndarray, *, on: float = FRAME_ON, off: float = FRAME_OFF,
                   smooth_s: float = SMOOTH_S) -> np.ndarray:  # fmt: skip
    size = max(1, int(round(smooth_s / FRAME_S)) | 1)
    return hysteresis(median_filter(p, size=size, mode="nearest"), on, off)


def beat_edges(grid: Grid, duration_s: float) -> tuple[int, np.ndarray]:
    """(number of the first beat, the times of the edges of every beat that overlaps the track), clipped to it. The
    grid extrapolates, so vocals before the first detected beat (a sung intro) still land on beats."""
    first, last = int(np.floor(grid.beat_at(0.0))), int(np.ceil(grid.beat_at(duration_s)))
    edges = np.asarray(grid.time_at(np.arange(first, max(last, first + 1) + 1, dtype=float)), float)
    return first, np.clip(edges, 0.0, duration_s)


def per_beat(values: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """Mean of a per-frame array over each beat."""
    idx = np.clip(np.round(edges / FRAME_S).astype(int), 0, values.size)
    c = np.concatenate(([0.0], np.cumsum(values, dtype=np.float64)))
    return (c[idx[1:]] - c[idx[:-1]]) / np.maximum(idx[1:] - idx[:-1], 1)


def beat_activity(active: np.ndarray, edges: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(share of each beat that is sung, whether the beat counts as active)."""
    share = per_beat(active.astype(np.float64), edges)
    return share, hysteresis(share, BEAT_ON, BEAT_OFF)


def _runs(on: np.ndarray, gap: int = 0) -> list[tuple[int, int]]:
    """[start, end) of the true runs, merged across gaps of up to `gap`."""
    idx = np.flatnonzero(on)
    if idx.size == 0:
        return []
    out: list[list[int]] = [[int(idx[0]), int(idx[0]) + 1]]
    for i in idx[1:]:
        if i - out[-1][1] <= gap:
            out[-1][1] = int(i) + 1
        else:
            out.append([int(i), int(i) + 1])
    return [(a, b) for a, b in out]


def classify(beats: int, seconds: float, frames: dict[str, np.ndarray], active: np.ndarray, lead_db: float) -> str:
    """The kind of one phrase. `frames` and `active` cover just the phrase; `lead_db` is the level of the track's
    loudest phrases against the mix."""
    bursts = _runs(active)
    voiced_s = float(active.sum()) * FRAME_S
    burst_s = float(np.median([b - a for a, b in bursts])) * FRAME_S if bursts else 0.0
    if beats <= CHOP_MAX_BEATS or (burst_s < CHOP_BURST_S and voiced_s < CHOP_MAX_DUTY * seconds):
        return "chop"
    on = active if active.any() else np.ones(active.size, bool)
    level = float(np.median((frames["vocal_db"] - frames["mix_db"])[on]))
    if level <= lead_db - BACKING_BELOW_LEAD_DB:
        return "backing"
    if level <= lead_db - BACKING_NEAR_LEAD_DB and float(np.median(frames["side"][on])) >= BACKING_MIN_SIDE:
        return "backing"
    if float(np.median(frames["harmonicity"][on])) < RAP_MAX_HARMONICITY:
        return "rap"
    return "lead"


def phrases(on: np.ndarray, first_beat: int, edges: np.ndarray, f: dict[str, np.ndarray],
            active: np.ndarray) -> list[dict]:  # fmt: skip
    """The vocal phrases: active beats merged across short gaps, each with its kind."""
    runs = _runs(on, PHRASE_GAP_BEATS)
    slices = [slice(*np.clip(np.round(edges[[a, b]] / FRAME_S).astype(int), 0, active.size)) for a, b in runs]
    rel = f["vocal_db"] - f["mix_db"]
    levels = [float(np.median(rel[s][active[s]])) if active[s].any() else -60.0 for s in slices]
    lead_db = float(np.percentile(levels, 75)) if levels else 0.0
    out = []
    for (a, b), s, level in zip(runs, slices, levels):
        fs = {k: v[s] for k, v in f.items()}
        seconds = float(edges[b] - edges[a])
        on_s = active[s] if active[s].any() else np.ones(s.stop - s.start, bool)
        out.append({"start_beat": first_beat + a, "end_beat": first_beat + b, "start_s": round(float(edges[a]), 3),
                    "end_s": round(float(edges[b]), 3), "kind": classify(b - a, seconds, fs, active[s], lead_db),
                    "level_db": round(level, 1), "harmonicity": round(float(np.median(fs["harmonicity"][on_s])), 2)})
    return out  # fmt: skip


def windows(on: np.ndarray, first_beat: int, edges: np.ndarray, f: dict[str, np.ndarray]) -> dict[str, list[dict]]:
    """`acapella`: active beats with what's under the vocal far below it. `instrumental`: music without vocals."""
    vocal, rest = per_beat(f["vocal_db"], edges), per_beat(f["rest_db"], edges)
    loud = float(np.percentile(rest, 90)) if rest.size else 0.0
    music = rest >= loud - MUSIC_FLOOR_DB

    def spans(mask: np.ndarray, min_beats: int) -> list[dict]:
        return [{"start_beat": first_beat + a, "end_beat": first_beat + b, "start_s": round(float(edges[a]), 3),
                 "end_s": round(float(edges[b]), 3)}
                for a, b in _runs(mask, WINDOW_GAP_BEATS) if b - a >= min_beats]  # fmt: skip

    return {"acapella": spans(on & (rest <= vocal - ACAPELLA_BELOW_VOCAL_DB), ACAPELLA_MIN_BEATS),
            "instrumental": spans(~on & music, INSTRUMENTAL_MIN_BEATS)}  # fmt: skip


def analyse_arrays(vocal: np.ndarray, rest: np.ndarray, grid: Grid, *, stems_model: str | None = None) -> dict:
    """The stored result, grid included: a re-analysed beatgrid can't leave the beat numbers meaning something else."""
    f = frame_features(vocal, rest)
    active = frame_activity(likelihood(f))
    duration = active.size * FRAME_S
    first, edges = beat_edges(grid, duration)
    share, on = beat_activity(active, edges)
    return {"version": VERSION, "stems_model": stems_model, "segments": grid.to_json(), "first_beat": first,
            "activity": np.round(share, 2).tolist(), "active": "".join("1" if v else "0" for v in on),
            "phrases": phrases(on, first, edges, f, active), **windows(on, first, edges, f),
            "duration_s": round(duration, 3)}  # fmt: skip


def read_stems(files: dict[str, Path]) -> tuple[np.ndarray, np.ndarray]:
    """(the vocal stem (2, n), the other stems summed to mono) at SAMPLE_RATE."""
    vocal = np.concatenate(list(decode_chunks(files[VOCAL_STEM], 1 << 20, channels=2))).reshape(-1, 2).T
    rest = np.zeros(vocal.shape[1], np.float32)
    for name, path in files.items():
        if name == VOCAL_STEM:
            continue
        x = np.concatenate(list(decode_chunks(path, 1 << 20)))[: rest.size]
        rest[: x.size] += x
    return vocal, rest


def analyse(folder: Path | str, grid: Grid, *, stems_model: str | None = None) -> dict:
    files = stem_files(folder)
    if files is None:
        raise ValueError(f"no stems in {folder}")
    vocal, rest = read_stems(files)
    return analyse_arrays(vocal, rest, grid, stems_model=stems_model)


def spans_s(data: dict) -> list[tuple[float, float]]:
    """The active beats as merged (start s, end s): what the evaluation scores."""
    edges = np.asarray(Grid(data["segments"]).time_at(data["first_beat"] + np.arange(len(data["active"]) + 1.0)), float)
    edges = np.clip(edges, 0.0, data["duration_s"])
    on = np.frombuffer(data["active"].encode(), np.uint8) == ord("1")
    return [(round(float(edges[a]), 3), round(float(edges[b]), 3)) for a, b in _runs(on)]


# --- the planner's inputs ---------------------------------------------------------------------------------------------


def _bar_values(data: dict, grid: Grid, first_downbeat_s: float, bar_s: float, bars: int) -> np.ndarray:
    """The activity over each of a constant grid's bars: the share of the bar's beats that are sung."""
    activity = np.asarray(data["activity"], float)
    if activity.size == 0 or bars <= 0:
        return np.zeros(max(bars, 0))
    edges = np.asarray(grid.beat_at(first_downbeat_s + bar_s * np.arange(bars + 1)), float) - data["first_beat"]
    c = np.concatenate(([0.0], np.cumsum(activity)))
    pos = np.clip(edges, 0.0, float(activity.size))
    whole = np.floor(pos).astype(int)
    frac = pos - whole
    cum = c[whole] + frac * activity[np.minimum(whole, activity.size - 1)]
    return (cum[1:] - cum[:-1]) / np.maximum(pos[1:] - pos[:-1], 1e-6) * (pos[1:] > pos[:-1])


def plan_input(an: dict, data: dict | None) -> dict:
    """A copy of a Seedbox 1 analysis (`structure.analyze`) for `musicreco.mixplan.generate`, with `vocal_per_bar` and
    the edges' `has_vocals` from the vocal stem. Without `data` the analysis comes back as it was, heuristic and all."""
    if not data:
        return an
    bars, fd, bar_s = int(an["bar_count"]), float(an["first_downbeat_s"]), float(an["bar_s"])
    values = _bar_values(data, Grid(data["segments"]), fd, bar_s, bars)
    out = {**an, "vocal_per_bar": [round(float(v), 3) for v in values], "vocal_source": "stems",
           "vocal_phrases": data["phrases"], "vocal_acapella": data["acapella"],
           "vocal_instrumental": data["instrumental"]}  # fmt: skip
    for name in ("intro", "outro"):
        edge = an.get(name)
        if not isinstance(edge, dict):
            continue
        n = int(edge.get("bars") or 0)
        lo = int(round((float(edge.get("start_s") or 0.0) - fd) / bar_s))
        if n <= 0:  # as structure.py: a 0-bar edge describes the first/last 8 bars
            lo, n = (0, EDGE_BARS) if name == "intro" else (max(0, bars - EDGE_BARS), EDGE_BARS)
        window = values[max(lo, 0) : lo + n]
        out[name] = {**edge, "has_vocals": bool(window.size and np.mean(window >= ON_BAR) >= EDGE_SHARE)}
    return out


def plan_input_for(lib: Any, hash_: str, an: dict) -> dict:
    """`plan_input` with what the library holds for the track; the analysis as it was until its stems are through."""
    row = lib.analysis(hash_, ANALYSER, VERSION)
    return plan_input(an, row["data"] if row else None)


# --- the job --------------------------------------------------------------------------------------------------------


@register("vocals", pool="light")
def vocals_job(payload: dict, ctx: Any) -> dict:
    """Payload `{"hash": ...}`: needs the track's stems and beats, and is queued by `on_job_finished` once it has
    both. Stems are evicted from a full cache but the analysis stays."""
    from seedbox2.analysis import stems

    hash_ = payload["hash"]
    lib = ctx.library
    folder = stems.current(lib, hash_)
    beats_row = lib.analysis(hash_, "beats")
    if folder is None or beats_row is None:
        return {"hash": hash_, "skipped": "needs stems" if folder is None else "needs beats"}
    model = _stems_model(lib, hash_)
    existing = lib.analysis(hash_, ANALYSER, VERSION)
    if existing is not None and existing["data"].get("stems_model") == model:
        return {"hash": hash_, "cached": True}
    ctx.progress(0.1)
    data = analyse(folder, Grid(beats_row["data"]["segments"]), stems_model=model)
    lib.put_analysis(hash_, ANALYSER, VERSION, data)
    ctx.progress(1.0)
    return {"hash": hash_, "phrases": len(data["phrases"]), "active_beats": data["active"].count("1")}


def _stems_model(lib: Any, hash_: str) -> str | None:
    from seedbox2.analysis.stems import ARTIFACT_KIND

    arts = lib.artifacts(hash_, ARTIFACT_KIND)
    return arts[-1]["model_version"] if arts else None


def submit(jobs: Any, hash_: str, *, priority: int | str = "library", front: bool = False) -> dict:
    return jobs.submit("vocals", {"hash": hash_}, priority=priority, front=front)


def on_job_finished(jobs: Any, lib: Any, row: dict) -> None:
    """The supervisor's listener: stems or beats arrived; once a track has both, its vocals can be worked out."""
    from seedbox2.analysis import stems

    payload = row.get("payload")
    if row.get("kind") not in ("stems", "beats") or row.get("state") != "done" or not isinstance(payload, dict):
        return
    hash_ = payload.get("hash")
    if hash_ and stems.current(lib, hash_) is not None and lib.analysis(hash_, "beats") is not None:
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        if lib.analysis(hash_, ANALYSER, VERSION) is None or (row["kind"] == "stems" and not result.get("cached")):
            submit(jobs, hash_)


def queue_missing(jobs: Any, lib: Any) -> int:
    """Queue the tracks that have stems and beats and no vocals yet (this ticket's first run, a new VERSION)."""
    hashes = lib.hashes_needing_analysis(ANALYSER, VERSION, artifact="stems", requires="beats")
    for h in hashes:
        submit(jobs, h, priority="background")
    return len(hashes)
