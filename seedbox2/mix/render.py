"""The offline renderer: a plan to audio, holding only what the plan plays (#69).

It sounds like Seedbox 1's renderer (`musicreco.mixrender`, whose stages and constants it shares; see its docstring for
the signal flow), and `tests/test_seedbox2_render.py` holds the two together. What differs is what is in memory:

- A deck decodes the parts of its file the window plays and its trim measures, not the whole file. The whole file's
  loudness comes from `track_lufs`: measured once in chunks, then kept in the cache folder.
- Decks go through the mixer in chunks (`stream.py`), with lanes at control rate, and sum into one bus.
- Per-sample positions exist a chunk at a time.

Rubber Band is the exception, and the bulk of the time and memory: `pedalboard.time_stretch` wants the whole deck and a
float64 ratio per sample. Everything about it is in `_stretched`, which the Signalsmith engine (#52) replaces.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from musicreco.mixplan import deck_bpm, deck_time, lane_value
from musicreco.mixrender import (
    _SINC, BITRATE, CEILING_DBTP, CTL, DECK_LUFS, ECHO_RETURN, ENCODE_TIMEOUT_S, FILTER_SMOOTH_S,
    LEVEL_RATIO, LEVEL_THRESHOLD_DB, MAKEUP_DB, MIN_MEASURE_S, OVERLAP_TRIM_CAP_DB, REVERB_RETURN, SEAM_S,
    SINC_PHASES, SINC_TAPS, STEP_RAMP_S, TRIM_LIMIT_DB, _aac_encoder, _clamp, _Clock, _deck_fx, _interp_ext,
    _overlap_beats, _pilot_map, _segments,
)  # fmt: skip
from musicreco.paths import ffmpeg_path

from . import convert, stream
from . import plan as v3
from .stream import CHUNK, SR

MERGE_GAP_S = 2.0  # parts of a file this close are decoded as one: a seek costs more than decoding this much

_lufs_lock = threading.Lock()


# --- the plan --------------------------------------------------------------------------------------------------------


def _deck_dict(d: v3.Deck) -> dict:
    """A v3 deck as the dict `musicreco.mixplan`'s and `mixrender`'s helpers read. The grid carries the timing, and
    native_bpm is only its first tempo."""
    raw = d.model_dump()
    return {"native_bpm": raw["grid"][0]["bpm"], "beat0_s": raw["grid"][0]["start_s"], **raw, **raw["sends"]}


def as_played(raw: object) -> tuple[dict, list[str]]:
    """A stored plan of either version as the renderer plays it, and notes on what it can't play. No deck has stems here
    yet (#53), so stem lanes fold into the EQ."""
    plan = raw if isinstance(raw, v3.MixPlan) else convert.load(raw)
    plan = v3.fold_stem_lanes(plan, lambda _: False)
    notes = [f"deck {n}: key shift isn't applied yet (#52)" for n, d in zip("AB", (plan.a, plan.b)) if d.key_shift]
    norm = {"window": plan.window.model_dump(), "tempo": [k.model_dump() for k in plan.tempo],
            "key_lock": plan.key_lock, "a": _deck_dict(plan.a), "b": _deck_dict(plan.b)}  # fmt: skip
    return norm, notes


# --- whole-track loudness --------------------------------------------------------------------------------------------


def _lufs_file() -> Path:
    folder = os.environ.get("SEEDBOX_CACHE_DIR", "").strip()
    if not folder:
        from seedbox2.paths import data_dirs

        folder = str(data_dirs()["SEEDBOX_CACHE_DIR"])
    return Path(folder) / "track-loudness.json"


def track_lufs(path: Path, sr: int = SR) -> float:
    """A file's integrated loudness. Measuring means decoding all of it, so it happens once per file: the result stays
    in the cache folder for as long as the file's size and mtime do."""
    try:
        st = Path(path).stat()
    except OSError as e:
        raise ValueError(f"could not decode {Path(path).name}: {e}") from e
    key = f"{Path(path).resolve()}|{st.st_size}|{st.st_mtime_ns}|{sr}"
    store = _lufs_file()
    with _lufs_lock:
        try:
            known = json.loads(store.read_text())
        except (OSError, ValueError):
            known = {}
        if isinstance(known, dict) and isinstance(known.get(key), (int, float)):
            return float(known[key])
    lufs = stream.measure_track(path, sr)
    with _lufs_lock:
        try:
            try:
                known = json.loads(store.read_text())
            except (OSError, ValueError):
                known = {}
            known = known if isinstance(known, dict) and len(known) < 20000 else {}
            known[key] = lufs
            store.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=store.parent, delete=False, suffix=".tmp") as f:
                json.dump(known, f)
            os.replace(f.name, store)
        except OSError:
            pass  # measured again next time
    return lufs


# --- a deck's source --------------------------------------------------------------------------------------------------


class _Parts:
    """The decoded parts of one file."""

    def __init__(self, path: Path, ranges: list[tuple[int, int]], sr: int) -> None:
        self.parts: list[tuple[int, np.ndarray]] = []
        self.end = math.inf  # the file's length in samples, once a part runs past it
        gap = int(MERGE_GAP_S * sr)
        merged: list[list[int]] = []
        for a, b in sorted(r for r in ranges if r[1] > r[0]):
            if merged and a <= merged[-1][1] + gap:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        for a, b in merged:
            a = max(0, a)
            if b <= a:
                continue
            audio, got = stream._read_window(path, a, b - a, sr)
            if got < b - a:
                self.end = min(self.end, a + got)
            self.parts.append((a, audio))

    def take(self, start: int, length: int) -> np.ndarray:
        seg = np.zeros((2, length), np.float32)
        for at, audio in self.parts:
            a, b = max(start, at), min(start + length, at + audio.shape[1])
            if a < b:
                seg[:, a - start : b - start] = audio[:, a - at : b - at]
        return seg


@dataclass
class _Layout:
    """Which samples of the file make up a deck's master beats [b0, b1)."""

    pieces: list[tuple[int, int, int]]  # (file sample, first output sample, end output sample)
    knot_beats: np.ndarray
    knot_samples: np.ndarray
    n: int


def _layout(deck: dict, b0: float, b1: float, sr: int) -> _Layout:
    """Each piece at its own tempo; sample = np.interp(beat, knot_beats, knot_samples)."""
    pieces = _segments(deck, b0, b1) or [(b0, b1, float(deck_time(deck, b0)), 60.0 / float(deck["native_bpm"]))]
    kb, ks = [b0], [0.0]
    for s, e, _, spb in pieces:
        kb.append(e)
        ks.append(ks[-1] + (e - s) * sr * spb)
    n = int(round(ks[-1]))
    out = []
    for idx, (_, _, ft, _) in enumerate(pieces):
        i0, i1 = int(round(ks[idx])), min(n, int(round(ks[idx + 1])))
        if i1 > i0:
            out.append((int(round(ft * sr)), i0, i1))
    return _Layout(out, np.array(kb), np.array(ks), n)


def _ranges(layout: _Layout, seam: int) -> list[tuple[int, int]]:
    return [(f0 - seam, f0 + i1 - i0) for f0, i0, i1 in layout.pieces]


def _source(parts: _Parts, layout: _Layout, seam: int) -> np.ndarray:
    """(2, n) audio for the layout; jumps are equal-power splices ending exactly on the seam, so the incoming beat's
    attack stays intact."""
    out = np.zeros((2, layout.n), np.float32)
    fade = np.linspace(0.0, 1.0, seam, endpoint=False) + 0.5 / seam
    f_in, f_out = np.sin(0.5 * np.pi * fade), np.cos(0.5 * np.pi * fade)
    prev_end = None
    for f0, i0, i1 in layout.pieces:
        out[:, i0:i1] = parts.take(f0, i1 - i0)
        if prev_end is not None and abs(f0 - prev_end) > 1 and i0 >= seam:
            out[:, i0 - seam : i0] = out[:, i0 - seam : i0] * f_out + parts.take(f0 - seam, seam) * f_in
        prev_end = f0 + (i1 - i0)
    return out


def _deck_trim_db(path: Path, deck: dict, parts: _Parts, trim_layout: _Layout | None, seam: int,
                  sr: int) -> tuple[float, float]:  # fmt: skip
    """(trim, whole-track trim) in dB: DECK_LUFS minus the deck's loudness over the range it's measured on (its source
    audio there, loops spliced in), within OVERLAP_TRIM_CAP_DB of the whole-track trim; the whole-track trim when that
    range is too short or silent."""
    whole = _clamp(DECK_LUFS - track_lufs(path, sr), -TRIM_LIMIT_DB, TRIM_LIMIT_DB)
    if trim_layout is None:
        return whole, whole
    src = _source(parts, trim_layout, seam)
    half = sr % 2 == 0
    meter = stream.Loudness(sr // 2 if half else sr)
    stream.feed_loudness(meter, src, 2 if half else 1)
    lufs = meter.ungated()
    if lufs <= -70:
        return whole, whole
    trim = _clamp(DECK_LUFS - lufs, whole - OVERLAP_TRIM_CAP_DB, whole + OVERLAP_TRIM_CAP_DB)
    return _clamp(trim, -TRIM_LIMIT_DB, TRIM_LIMIT_DB), whole


# --- a deck on the master clock ---------------------------------------------------------------------------------------


class _Warp:
    """Reads audio at fractional positions with a 32-tap Kaiser-windowed sinc (zeros outside it)."""

    def __init__(self, y: np.ndarray) -> None:
        half = SINC_TAPS // 2
        self.n = y.shape[1]
        self.pad = np.pad(y, ((0, 0), (half + 1, half + 2)))
        self.lim = self.pad.shape[1] - SINC_TAPS - 1
        self.taps = np.arange(SINC_TAPS)

    def __call__(self, pos: np.ndarray) -> np.ndarray:
        half = SINC_TAPS // 2
        i = np.floor(pos)
        scaled = ((pos - i) * SINC_PHASES).round().astype(np.int64)
        ph = np.minimum(scaled, SINC_PHASES - 1)
        i = i.astype(np.int64) + (scaled >= SINC_PHASES)
        idx = np.clip(i + 2, 0, self.lim)[:, None] + self.taps[None, :]
        w = _SINC[ph]
        out = np.empty((2, pos.size), np.float32)
        for ch in range(2):
            out[ch] = np.einsum("ij,ij->i", self.pad[ch][idx], w)
        bad = (pos < -half) | (pos > self.n + half)
        if bad.any():
            out[:, bad] = 0.0
        return out


def _stretched(src: np.ndarray, speed: np.ndarray, sr: int) -> tuple[_Warp, Callable[[np.ndarray], np.ndarray], str]:
    """Rubber Band with key lock, and a timing correction: a click track stretched with the same ratios measures where
    every source sample landed. (reader, source position -> position in its output, note)."""
    import pedalboard

    y = pedalboard.time_stretch(src, sr, stretch_factor=speed, high_quality=True, transient_mode="crisp")
    nominal = np.concatenate([[0.0], np.cumsum(1.0 / speed)])[:-1]
    m = _pilot_map(speed, sr, nominal)
    note = ""
    if m is None:
        m = (np.arange(0, speed.size, 1024, dtype=np.float64), nominal[::1024])
        note = "time-stretch timing could not be verified (beats may drift by a few ms)"
    c, p = m
    slope_lo = (p[1] - p[0]) / (c[1] - c[0])
    slope_hi = (p[-1] - p[-2]) / (c[-1] - c[-2])

    def where(jq: np.ndarray) -> np.ndarray:
        u = np.interp(jq, c, p)
        u = np.where(jq < c[0], p[0] + (jq - c[0]) * slope_lo, u)
        return np.where(jq > c[-1], p[-1] + (jq - c[-1]) * slope_hi, u)

    return _Warp(y), where, note


@dataclass
class _Deck:
    """One deck, stretched and on the master clock, ready for the mixer."""

    name: str
    deck: dict
    x: np.ndarray
    qa: int
    qb: int
    warnings: list[str] = field(default_factory=list)


def _edge_speed(deck: dict, clock: _Clock, beats: np.ndarray, p0: float, p1: float) -> np.ndarray:
    # the padding beats around the play range are silent: stretch them as the edges are, so a deck that plays at its
    # own tempo up to (or from) a tempo jump on a cut isn't time-stretched at all
    edge = min(1.0 / 64, (p1 - p0) / 4)
    held = np.clip(beats, p0 + edge, p1 - edge)
    return clock.bpm(held) / np.asarray(deck_bpm(deck, held), dtype=np.float64)


def _deck_on_master(name: str, deck: dict, holder: list[np.ndarray], file_end: float, layout: _Layout, trim: float,
                    clock: _Clock, w0: float, w1: float, m_total: int, sr: int, key_lock: bool) -> _Deck:  # fmt: skip
    """The deck through stretch, gain, gate and effects, from master sample qa to qb. Its source audio comes in `holder`,
    a list it takes it out of, so that it can let go of it as soon as it's been read. A file ending at sample `file_end`
    (inf if the deck stops before that) gets a warning if the deck plays past it."""
    src = holder.pop()
    p0, p1 = max(deck["play_from_beat"], w0), min(deck["play_to_beat"], w1)
    warn: list[str] = []
    ft = np.asarray(deck_time(deck, np.linspace(p0, p1, 64, endpoint=False)), dtype=np.float64)  # play_to is exclusive
    if ft.min() < -0.05 or ft.max() > file_end / sr + 0.05:
        warn.append("plays outside its audio file (silence there)")
    n = src.shape[1]
    kb, ks = layout.knot_beats, layout.knot_samples
    # the ratio changes slowly: this finds out whether it changes at all without a value per sample
    coarse = _edge_speed(deck, clock, _interp_ext(np.arange(0, n + 256, 256, dtype=np.float64), ks, kb), p0, p1)
    audible = np.abs(coarse - 1)
    if audible.size and audible.max() > 0.08:
        warn.append(f"tempo changed by up to {100 * audible.max():.0f}% (may sound stretched)")
    fade_in = int(round(0.003 * sr)) if p0 > w0 else 0
    fade_out = int(round(0.004 * sr)) if p1 < w1 else 0
    t0, t1 = float(clock.time(p0)) * sr, float(clock.time(p1)) * sr
    qa = max(0, int(math.floor(t0)) - fade_in)
    qb = min(m_total, int(math.ceil(t1)) + int(0.25 * sr))
    x = np.zeros((2, qb - qa), np.float32)

    def jq_at(a: int, b: int) -> np.ndarray:
        return _interp_ext(clock.beat(np.arange(qa + a, qa + b) / sr), kb, ks)

    # 3 cases: at its own tempo it's copied, without key lock it's read at the right speed, and with key lock stretched
    where = None
    if audible.max() >= 1e-9:
        if not key_lock:
            reader = _Warp(src)
            del src
        else:
            speed = np.empty(n, np.float64)
            for a in range(0, n, CHUNK):
                beats = _interp_ext(np.arange(a, min(n, a + CHUNK), dtype=np.float64), ks, kb)
                speed[a : a + CHUNK] = _edge_speed(deck, clock, beats, p0, p1)
            reader, where, note = _stretched(src, np.clip(speed, 0.5, 2.0), sr)
            if note:
                warn.append(note)
            del speed, src
        for a in range(0, qb - qa, CHUNK):
            b = min(qb - qa, a + CHUNK)
            jq = jq_at(a, b)
            x[:, a:b] = reader(where(jq) if where else jq)
        del reader
    else:
        anchor = min(max(int(round(t0)) - qa, 0), qb - qa - 1)
        start = int(round(float(jq_at(anchor, anchor + 1)[0]))) - anchor
        a, b = max(0, -start), min(qb - qa, n - start)
        if a < b:
            x[:, a:b] = src[:, start + a : start + b]
        del src
    # gate: the deck sounds on [p0, p1]
    gain = np.float32(trim)
    for a in range(0, qb - qa, CHUNK):
        b = min(qb - qa, a + CHUNK)
        q = np.arange(qa + a, qa + b, dtype=np.float64)
        g = np.ones(b - a, np.float32)
        if fade_in:
            g = np.minimum(g, np.clip((q - (t0 - fade_in)) / fade_in, 0, 1).astype(np.float32))
        g = np.minimum(g, np.clip((t1 - q) / max(fade_out, 1), 0, 1).astype(np.float32))
        x[:, a:b] *= (np.sin(0.5 * np.pi * g) ** 2).astype(np.float32) * gain
    return _Deck(name, deck, _deck_fx(x, deck.get("fx") or [], clock, qa, sr), qa, qb, warn)


# --- the mixer --------------------------------------------------------------------------------------------------------


def _lanes(d: _Deck, clock: _Clock, sr: int):
    ctl_beats = clock.beat((d.qa + np.arange(0, d.qb - d.qa + CTL, CTL)) / sr)

    def lane(k: str, smooth: float = STEP_RAMP_S) -> stream.Lane:
        return stream.Lane(lane_value(d.deck["lanes"][k], ctl_beats), CTL, smooth, sr)

    return lane


def _iso_gains(lane: stream.Lane, a: int, b: int) -> np.ndarray | np.float32:
    return stream.iso_gain(np.float32(lane.flat)) if lane.flat is not None else stream.iso_gain(lane.at(a, b))


class _Send:
    """A deck's echo or reverb return: starts at the send's first sound and rings on to the end of the window."""

    def __init__(self, name: str, lane: stream.Lane, tap: str, effect: Callable) -> None:
        self.name, self.lane, self.tap, self.effect = name, lane, tap, effect
        self.started = False
        self.stem: np.ndarray | None = None


def _mix_deck(d: _Deck, clock: _Clock, dry_mix: np.ndarray, fx: np.ndarray, m_total: int, sr: int,
              stems: dict[str, np.ndarray] | None) -> None:  # fmt: skip
    """Runs the deck through EQ, filter and fader into `dry_mix`, and its sends into `fx`, a chunk at a time."""
    deck, qa, n = d.deck, d.qa, d.qb - d.qa
    lanes = deck["lanes"]
    lane = _lanes(d, clock, sr)
    volume = lane("volume")
    iso = None
    if any(kf["value"] != 0 for k in ("eq_low", "eq_mid", "eq_high") for kf in lanes[k]):
        iso = stream.Isolator(sr), lane("eq_low"), lane("eq_mid"), lane("eq_high")
    dj = None
    if any(kf["value"] != 0 for kf in lanes["filter"]):
        res = lane("filter_res", FILTER_SMOOTH_S) if any(kf["value"] > 0 for kf in lanes["filter_res"]) else None
        dj = stream.DjFilter(lane("filter", FILTER_SMOOTH_S), res, n, sr)

    sends: list[_Send] = []
    delay_at = fb_lane = None
    if any(kf["value"] > 0 for kf in lanes["echo_send"]):
        fb_lane = lane("echo_feedback")
        last = deck["play_to_beat"] - 1.0 / 64
        echo_beats = deck["echo_beats"]
        # repeats keep the deck's tempo once it stops: a tempo jump on the cut would make the delay line jump
        grid_bpm = clock.bpm(np.minimum(clock.grid, last))
        max_delay = echo_beats * 60.0 / float(grid_bpm.min()) * sr
        echo = stream.Echo(max_delay, sr)

        def delay_at(a: int, b: int) -> np.ndarray:
            beats = np.minimum(clock.beat(np.arange(a, b) / sr), last)
            return echo_beats * 60.0 / clock.bpm(beats) * sr

        sends.append(_Send("echo", lane("echo_send"), deck["echo_tap"], echo))
    if any(kf["value"] > 0 for kf in lanes["reverb_send"]):
        sends.append(_Send("reverb", lane("reverb_send"), deck["reverb_tap"], stream.Reverb(deck["reverb_size"], sr)))

    dry_stem = None
    if stems is not None:
        dry_stem = np.zeros((2, m_total), np.float32)
        stems[d.name] = dry_stem
    for a in range(0, n if not sends else m_total - qa, CHUNK):
        b = min(a + CHUNK, m_total - qa)
        inside = min(b, n)
        c = d.x[:, a:inside] if a < n else None
        dry = None
        if c is not None:
            y = c
            if iso is not None:
                y = iso[0](y, *(_iso_gains(band, a, inside) for band in iso[1:]))
            if dj is not None:
                y = dj(y, a)
            dry = y * volume.at(a, inside)
            dry_mix[:, qa + a : qa + inside] += dry
            if dry_stem is not None:
                dry_stem[:, qa + a : qa + inside] = dry
        for s in sends:
            size = b - a
            start = 0
            send = np.zeros((2, size), np.float32)
            if c is not None:
                level = s.lane.at(a, inside)
                if not s.started:
                    loud = level > 1e-4
                    if not loud.any():
                        continue
                    start = int(np.argmax(loud))
                    s.started = True
                send[:, start : inside - a] = (c if s.tap == "pre_eq" else dry)[:, start:] * level[start:]
            elif not s.started:
                continue
            at0, at1 = qa + a + start, qa + b
            if s.name == "echo":
                fb = np.full(size - start, fb_lane.v[-1], np.float32)
                if c is not None:
                    fb[: inside - a - start] = fb_lane.at(a + start, inside)
                ret = s.effect(send[:, start:], delay_at(at0, at1), fb) * np.float32(ECHO_RETURN)
            else:
                ret = s.effect(send[:, start:]) * np.float32(REVERB_RETURN)
            fx[:, at0:at1] += ret
            if stems is not None:
                if s.stem is None:
                    s.stem = stems[f"{d.name} {s.name}"] = np.zeros((2, m_total), np.float32)
                s.stem[:, at0:at1] = ret


# --- render ----------------------------------------------------------------------------------------------------------


def _prepare(name: str, path: Path, deck: dict, beats: tuple[float, float], clock: _Clock, w0: float, w1: float,
             m_total: int, sr: int, key_lock: bool) -> tuple[tuple[float, float], _Deck | None]:  # fmt: skip
    """Decodes what the deck needs; (its trims in dB, the deck on the master clock)."""
    seam = max(8, int(round(SEAM_S * sr)))
    p0, p1 = max(deck["play_from_beat"], w0), min(deck["play_to_beat"], w1)
    layout = _layout(deck, p0 - 2.0, p1 + 1.0, sr) if p1 > p0 else None
    b0, b1 = beats
    measured = (b1 - b0) * 60.0 / float(deck["native_bpm"]) >= MIN_MEASURE_S
    trim_layout = _layout(deck, b0, b1, sr) if measured else None
    ranges = [r for lay in (layout, trim_layout) if lay is not None for r in _ranges(lay, seam)]
    parts = _Parts(Path(path), ranges, sr)
    trims = _deck_trim_db(Path(path), deck, parts, trim_layout, seam, sr)
    if layout is None:
        return trims, None
    holder, file_end = [_source(parts, layout, seam)], parts.end
    del parts  # what's decoded is in the source now
    gain = 10 ** (trims[0] / 20)
    return trims, _deck_on_master(name, deck, holder, file_end, layout, gain, clock, w0, w1, m_total, sr, key_lock)


def render_audio(plan: object, a_path: Path, b_path: Path, sr: int = SR,
                 progress: Callable[[float], None] | None = None, keep_stems: bool = False) -> dict:  # fmt: skip
    """Everything `render` does except encoding: the master mix and the numbers. `keep_stems` also keeps each deck's and
    return's signal ("stems") and the leveller's gain ("gain"), a whole window each, for tests and metrics."""

    def report(f: float) -> None:
        if progress is not None:
            try:
                progress(f)
            except Exception:  # noqa: BLE001 - a progress sink must never break a render
                pass

    norm, warnings = as_played(plan)
    w0, w1 = norm["window"]["start_beat"], norm["window"]["end_beat"]
    decks = [norm["a"], norm["b"]]
    lo = min([w0] + [max(d["play_from_beat"], w0) - 3 for d in decks])
    clock = _Clock(norm["tempo"], lo, w1 + 2, w0)
    duration = float(clock.time(w1))
    m_total = int(round(duration * sr))
    if m_total < sr // 10:
        raise ValueError("plan window is too short to render")
    if duration > 600:
        raise ValueError("plan window is longer than 10 minutes")
    report(0.02)
    with ThreadPoolExecutor(2) as pool:
        ready = list(pool.map(lambda args: _prepare(*args, clock, w0, w1, m_total, sr, norm["key_lock"]),
                              zip("AB", (a_path, b_path), decks, _overlap_beats(decks, w0, w1))))  # fmt: skip
    trims_db = [t for t, _ in ready]
    states = [d for _, d in ready]
    del ready
    report(0.7)
    dry_mix = np.zeros((2, m_total), np.float32)
    fx = np.zeros((2, m_total), np.float32)
    stems: dict[str, np.ndarray] | None = {} if keep_stems else None
    for i, st in enumerate(states):
        if st is None:
            continue
        warnings += [f"deck {st.name}: {w}" for w in st.warnings]
        _mix_deck(st, clock, dry_mix, fx, m_total, sr, stems)
        states[i] = None  # its signal goes now
    report(0.8)
    # the leveller only tames the dry decks' blend bump; echo and reverb returns ride on top of it unlevelled
    leveller = stream.Leveller(10 ** ((DECK_LUFS + 0.691) / 10), LEVEL_THRESHOLD_DB, LEVEL_RATIO, MAKEUP_DB, sr)
    for a in range(0, m_total, CHUNK):
        leveller.feed(dry_mix[:, a : a + CHUNK])
    lev_red = leveller.finish()
    gain = np.empty(m_total, np.float32) if keep_stems else None
    makeup = np.float32(10 ** (MAKEUP_DB / 20))
    mix = dry_mix
    for a in range(0, m_total, CHUNK):
        b = min(m_total, a + CHUNK)
        g = leveller.gain(a, b)
        mix[:, a:b] = mix[:, a:b] * g + fx[:, a:b] * makeup
        if gain is not None:
            gain[a:b] = g
    del fx
    lim_red = stream.limit(mix, CEILING_DBTP, sr)
    fi, fo = int(0.005 * sr), int(0.03 * sr)
    mix[:, :fi] *= np.linspace(0, 1, fi, dtype=np.float32)
    mix[:, -fo:] *= np.linspace(1, 0, fo, dtype=np.float32)
    if lim_red > 8:
        warnings.append(f"limiter worked hard ({lim_red:.1f} dB): the overlap is very loud")
    beats = np.arange(math.ceil(w0 - 1e-9), math.floor(w1 + 1e-9) + 1)
    report(0.9)
    meter = stream.Loudness(sr)
    stream.feed_loudness(meter, mix)
    out = {
        "mix": mix, "sr": sr, "clock": clock, "norm": norm,
        "duration_s": round(m_total / sr, 4),
        "start_beat": w0,
        "beat_times_s": [round(float(t), 4) for t in clock.time(beats)],
        "lufs": round(meter.integrated(), 2),
        "true_peak_db": round(stream.true_peak_db(mix), 2),
        "warnings": warnings,
        "leveller_db": round(lev_red, 2), "limiter_db": round(lim_red, 2),
        "trim_db": {nm: round(t, 2) for nm, (t, _) in zip("AB", trims_db)},
        "track_trim_db": {nm: round(w, 2) for nm, (_, w) in zip("AB", trims_db)},
    }  # fmt: skip
    if keep_stems:
        out |= {"stems": stems, "gain": gain}
    return out


def encode_m4a(audio: np.ndarray, sr: int, out_path: Path) -> None:
    """(2, n) float32 -> AAC in an m4a at `out_path`, handed to ffmpeg a chunk at a time."""
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise ValueError("ffmpeg not found")
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "f32le", "-ar", str(sr),
           "-ac", "2", "-i", "-", "-c:a", _aac_encoder(ffmpeg), "-b:a", BITRATE, "-movflags", "+faststart",
           "-f", "ipod", str(out_path)]  # fmt: skip
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
        except OSError as e:
            raise ValueError(f"could not encode the render: {e}") from e
        timer = threading.Timer(ENCODE_TIMEOUT_S, proc.kill)
        timer.start()
        try:
            try:
                for a in range(0, audio.shape[1], CHUNK):
                    proc.stdin.write(np.clip(audio[:, a : a + CHUNK], -1.0, 1.0).T.astype("<f4").tobytes())
                proc.stdin.close()
            except OSError:
                pass  # ffmpeg stopped reading: its exit code says why
            code = proc.wait()
        finally:
            timer.cancel()
        if code != 0:
            err.seek(0)
            tail = err.read().decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
            raise ValueError(f"could not encode the render: {tail[0]}")


def render(plan: object, a_path: Path, b_path: Path, out_path: Path, sr: int = SR,
           progress: Callable[[float], None] | None = None) -> dict:  # fmt: skip
    """Renders `plan` (deck a from a_path, deck b from b_path) to an AAC m4a at out_path. The numbers, without the
    audio. ValueError for an unusable plan or undecodable audio."""
    r = render_audio(plan, a_path, b_path, sr, progress)
    encode_m4a(r["mix"], sr, Path(out_path))
    if progress is not None:
        try:
            progress(1.0)
        except Exception:  # noqa: BLE001
            pass
    return {k: r[k] for k in ("duration_s", "start_beat", "beat_times_s", "lufs", "true_peak_db", "warnings")}
