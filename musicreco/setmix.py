"""Full set mix: every transition's MixPlan back to back as one continuous mix (MP3 + .cue + tracklist).

Timeline. All plans go on one GLOBAL beat axis: plan i (track i -> track i+1) sits at G = O_i + c_i * x for its
master beats x. c_i rescales half/double-time pairs (c_0 = 1, c_i = c_{i-1} * B's native / A's native) so every track
keeps one beat unit for its whole life. A track runs on the same grid in the plan that brings it in (deck B) and the
plan that takes it out (deck A), which fixes O_i: the number of the track's own beats between its cue and its out
point. Each track becomes ONE deck on that axis, with the B-side automation (loops, lanes) up to a split point
between its two transitions and the A-side automation after it.

Tempo. Inside each blend the plan's own tempo lane applies; between blends the tempo eases from one meeting tempo to
the next across the track's solo stretch, so tempo moves are spread over minutes (set-wide continuity) instead of
each track snapping back to its native BPM between transitions.

Rendering streams: decks in order, each in ~32-bar chunks whose edges stay clear of automation, through Rubber Band
key-lock stretch with mixrender's pilot timing correction, deck FX, filter, pre-EQ echo / reverb sends (rendered per
send region with their tails: both effects are linear, so the pieces sum exactly), the isolator (state carried
across chunks) and the fader. The summed mix goes through mixrender's leveller and true-peak limiter with lookahead
context, then straight into ffmpeg (MP3 320 kbps), so memory stays bounded for hour-long sets.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import subprocess
import tempfile
import threading
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import signal

from . import mixplan, mixrender
from .models import Track
from .paths import ffmpeg_path

log = logging.getLogger(__name__)

SETMIX_VERSION = 1
SR = mixrender.SR
CHUNK_BEATS = 64  # ~16 bars per stretch chunk (several stretch in parallel)
XFADE = 256  # samples: equal-power splice between stretch chunks, ending on a beat line
SPAN_MARGIN = 8.0  # beats of clearance between chunk edges and any automation
BUS_CONTEXT_S = 4.0  # leveller/limiter context on each side of a processed block
BUS_BLOCK_S = 20.0
ECHO_TAIL_MAX_S = 14.0
REVERB_TAIL_S = 7.0
END_TAIL_S = 3.0  # silence kept after the last track for FX tails
START_DOWNBEAT_MAX_S = 2.0  # start the first track on its first downbeat when that comes this early
BITRATE = "320k"
ENCODE_TIMEOUT_S = 120
STRETCH_WORKERS = max(1, min(3, (os.cpu_count() or 2) - 1))  # Rubber Band releases the GIL: chunks stretch ahead


class Cancelled(Exception):
    """The render was cancelled."""


@dataclass
class SetEntry:
    track: Track
    path: Path
    analysis: dict | None = None
    folder: str = ""


@dataclass
class _Deck:
    index: int
    entry: SetEntry
    deck: dict  # mixrender-normalised deck on the global axis
    p0: float  # global play range, beats
    p1: float
    spans: list[tuple[float, float]]  # automation spans, global beats


@dataclass
class Timeline:
    decks: list[_Deck]
    tempo: list[dict]
    g_start: float
    g_end: float
    marks: list[dict]  # per track: index, in_beat (enters), take_beat (takes over), out_beat (leaves)
    techniques: list[dict]  # per transition: index, technique, bars, beat
    conflicts: list[int] = field(default_factory=list)  # tracks too short for both of their transitions
    warnings: list[str] = field(default_factory=list)


# --- timeline -------------------------------------------------------------------------------------------------------


def _lane_span(kfs: list[dict]) -> tuple[float, float] | None:
    """(first, last) beat where a lane moves: from the keyframe a change starts at to the last change."""
    first = last = None
    for p, q in zip(kfs, kfs[1:]):
        if abs(q["value"] - p["value"]) > 1e-9:
            first = p["beat"] if first is None else first
            last = q["beat"]
    return None if first is None else (first, last)


def _automation(deck: dict) -> list[tuple[float, float]]:
    """Spans (plan beats) where a deck's lanes move, its loops run or its FX play."""
    out = [s for lane in mixplan.LANES if (s := _lane_span(deck["lanes"][lane])) is not None]
    out += [(lp["start_beat"], lp["end_beat"]) for lp in deck["loops"]]
    out += [(f["beat"], f["beat"] + f["beats"]) for f in deck.get("fx") or []]
    return out


def _lane_slice(kfs: list[dict], lo: float, hi: float, o: float, c: float) -> list[dict]:
    """A plan lane mapped to global beats (G = o + c * x) and limited to [lo, hi)."""
    g = [{"beat": o + c * k["beat"], "value": k["value"], "curve": k["curve"]} for k in kfs]
    out = []
    if math.isfinite(lo):
        out.append({"beat": lo, "value": float(mixplan.lane_value(g, lo)), "curve": "hold"})
    out += [k for k in g if lo < k["beat"] < hi]
    if math.isfinite(hi):
        out.append({"beat": hi, "value": float(mixplan.lane_value(g, hi - 1e-9)), "curve": "hold"})
    return out or [{"beat": 0.0, "value": kfs[0]["value"], "curve": "hold"}]


def _start_time(entry: SetEntry) -> float:
    an = entry.analysis or {}
    fd = an.get("first_downbeat_s")
    return float(fd) if isinstance(fd, (int, float)) and 0 <= fd <= START_DOWNBEAT_MAX_S else 0.0


def _end_time(entry: SetEntry) -> float:
    an = entry.analysis or {}
    d = an.get("duration_s")
    if isinstance(d, (int, float)) and d > 0:
        return float(d)
    return float(entry.track.duration_s or 0.0)


def build(entries: list[SetEntry], plans: list[dict]) -> Timeline:
    """The set's timeline: one deck per track on the global beat axis, the global tempo lane, tracklist marks."""
    n = len(entries)
    if n < 2:
        raise ValueError("a set mix needs at least two tracks")
    if len(plans) != n - 1:
        raise ValueError("a set mix needs one mix plan per transition")
    plans = [mixplan.validate(p) for p in plans]
    for i, p in enumerate(plans):
        if p["from_id"] != entries[i].track.id or p["to_id"] != entries[i + 1].track.id:
            raise ValueError(f"mix plan {i + 1} is for another pair of tracks")
    warnings: list[str] = []

    # beat scales (half/double-time) and offsets
    c = [1.0]
    for i in range(1, n - 1):
        r = plans[i - 1]["b"]["native_bpm"] / plans[i]["a"]["native_bpm"]
        snap = min((0.5, 1.0, 2.0), key=lambda k: abs(r / k - 1))
        c.append(c[-1] * (snap if abs(r / snap - 1) < 0.01 else r))
    nb = [plans[0]["a"]["native_bpm"] * c[0]] + [plans[i - 1]["b"]["native_bpm"] * c[i - 1] for i in range(1, n)]
    O = [0.0]
    for i in range(1, n - 1):
        O.append(O[-1] + (plans[i]["a"]["beat0_s"] - plans[i - 1]["b"]["beat0_s"]) * nb[i] / 60.0)

    def g(i: int, x: float) -> float:
        return O[i] + c[i] * x

    decks: list[_Deck] = []
    conflicts: list[int] = []
    marks: list[dict] = []
    for i, e in enumerate(entries):
        side_b = plans[i - 1]["b"] if i > 0 else None
        side_a = plans[i]["a"] if i < n - 1 else None
        if side_a is not None:
            beat0 = side_a["beat0_s"] - O[i] * 60.0 / nb[i]
        else:
            beat0 = side_b["beat0_s"] - O[i - 1] * 60.0 / nb[i]
        spans_b = [(g(i - 1, s), g(i - 1, t)) for s, t in _automation(side_b)] if side_b else []
        spans_a = [(g(i, s), g(i, t)) for s, t in _automation(side_a)] if side_a else []
        b_end = max((t for _, t in spans_b), default=-math.inf)
        a_start = min((s for s, _ in spans_a), default=math.inf)
        if side_b and side_a:
            if b_end <= a_start:
                split = 0.5 * (b_end + a_start)
            else:  # the out-transition starts before the in-transition is over
                core_b = g(i - 1, max(0.0, plans[i - 1]["a"]["play_to_beat"]))
                core_a = g(i, min([0.0] + [lp["start_beat"] for lp in side_a["loops"]]))
                if core_b > core_a:
                    conflicts.append(i)
                split = a_start
                warnings.append(f"track {i + 1} ({e.track.display_name}) is short for both of its transitions — "
                                "some automation overlaps")
        else:
            split = math.inf if side_b else -math.inf
        lanes = {}
        for lane in mixplan.LANES:
            if side_b and side_a:
                kb = _lane_slice(side_b["lanes"][lane], -math.inf, split, O[i - 1], c[i - 1])
                ka = _lane_slice(side_a["lanes"][lane], split, math.inf, O[i], c[i])
                lanes[lane] = kb + ka
            elif side_b:
                lanes[lane] = _lane_slice(side_b["lanes"][lane], -math.inf, math.inf, O[i - 1], c[i - 1])
            else:
                lanes[lane] = _lane_slice(side_a["lanes"][lane], -math.inf, math.inf, O[i], c[i])
        loops = []
        if side_b:
            loops += [{"start_beat": g(i - 1, lp["start_beat"]), "end_beat": g(i - 1, lp["end_beat"]),
                       "source_beat": g(i - 1, lp["source_beat"]), "length_beats": c[i - 1] * lp["length_beats"]}
                      for lp in side_b["loops"] if g(i - 1, lp["start_beat"]) < split]
        if side_a:
            loops += [{"start_beat": g(i, lp["start_beat"]), "end_beat": g(i, lp["end_beat"]),
                       "source_beat": g(i, lp["source_beat"]), "length_beats": c[i] * lp["length_beats"]}
                      for lp in side_a["loops"] if g(i, lp["end_beat"]) > split]
        loops.sort(key=lambda lp: lp["start_beat"])
        clean = []
        for lp in loops:  # drop loops that collide (only possible on a conflicting short track)
            if not clean or lp["start_beat"] >= clean[-1]["end_beat"] - 1e-9:
                clean.append(lp)
        fx = [{"kind": f["kind"], "beat": g(i, f["beat"]), "beats": c[i] * f["beats"]}
              for f in (side_a.get("fx") or [] if side_a else [])]
        p0 = g(i - 1, side_b["play_from_beat"]) if side_b else (_start_time(e) - beat0) * nb[i] / 60.0
        if side_a:
            p1 = g(i, side_a["play_to_beat"])
        else:
            p1 = (_end_time(e) - beat0) * nb[i] / 60.0
        if p1 <= p0 + 1:
            raise ValueError(f"track {i + 1} ({e.track.display_name}) has nothing left to play between its transitions")
        echo = (side_a or side_b)["echo_beats"] * (c[i] if side_a else c[i - 1])
        deck = {"track_id": e.track.id, "beat0_s": beat0, "native_bpm": nb[i], "play_from_beat": p0,
                "play_to_beat": p1, "echo_beats": max(0.0625, min(8.0, echo)), "loops": clean, "fx": fx,
                "lanes": lanes}
        decks.append(_Deck(i, e, deck, p0, p1, sorted(spans_b + spans_a)))
        if i == 0:
            marks.append({"index": 0, "in_beat": p0, "take_beat": p0})
        else:
            pl = plans[i - 1]
            E = max(0.0, pl["a"]["play_to_beat"])
            swap = next((mk["beat"] for mk in pl["markers"] if mk["label"] == "bass swap"), None)
            take = swap if swap is not None else E / 2
            marks.append({"index": i, "in_beat": g(i - 1, 0.0), "take_beat": g(i - 1, take)})
    for i, mk in enumerate(marks):
        mk["out_beat"] = decks[i].p1

    # tempo: the plans' own tempo inside each blend, eased across each track's solo stretch in between
    tk: list[dict] = [{"beat": decks[0].p0, "value": nb[0], "curve": "ease"}]
    for i, p in enumerate(plans):
        T = p["tempo"]
        E = max(0.0, p["a"]["play_to_beat"])  # blend end (0 for the cuts)
        v_before = float(mixplan.lane_value(T, -1e-4)) * c[i]
        v_in = float(mixplan.lane_value(T, 0.0)) * c[i]
        tk.append({"beat": O[i], "value": v_before, "curve": "hold"})
        if abs(v_in - v_before) > 1e-6:
            tk.append({"beat": O[i], "value": v_in, "curve": "hold"})
        tk += [{"beat": g(i, k["beat"]), "value": k["value"] * c[i], "curve": k["curve"]} for k in T
               if 0 < k["beat"] < E]
        if E > 0:
            tk.append({"beat": g(i, E), "value": float(mixplan.lane_value(T, E)) * c[i], "curve": "ease"})
        else:
            tk[-1] = {**tk[-1], "curve": "ease"}
    tk.append({"beat": decks[-1].p1, "value": nb[-1], "curve": "hold"})
    tk.sort(key=lambda k: k["beat"])
    for k in tk:
        k["value"] = min(mixplan.TEMPO_RANGE[1], max(mixplan.TEMPO_RANGE[0], k["value"]))
    techniques = []
    for i, p in enumerate(plans):
        overlap = 0 if p["a"].get("fx") else int(round(max(0.0, p["a"]["play_to_beat"]) / 4))
        techniques.append({"index": i, "technique": p["technique"], "bars": p["bars"], "overlap": overlap,
                           "beat": g(i, 0.0), "edited": bool(p.get("edited"))})
    return Timeline(decks, tk, decks[0].p0, decks[-1].p1, marks, techniques, conflicts, warnings)


def set_hash(entries: list[SetEntry], plans: list[dict]) -> str:
    """Cache key: every plan's audio-relevant part + every file's identity + versions. 24 hex chars."""
    norm = []
    for p in plans:
        try:
            norm.append(mixrender._round_floats(mixrender._normalise(mixplan.validate(p))))
        except ValueError:
            norm.append({"invalid": json.dumps(p, sort_keys=True, default=str)})
    payload = {"v": [SETMIX_VERSION, mixrender.RENDER_VERSION, mixplan.PLAN_VERSION], "plans": norm,
               "files": [mixrender._file_identity(e.path) for e in entries]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


def summary(tl: Timeline, entries: list[SetEntry]) -> dict:
    """Tracklist + length of a timeline without rendering it (the clock alone)."""
    clock = mixrender._Clock(tl.tempo, tl.g_start - 4, tl.g_end + 4, tl.g_start)
    t = lambda beat: round(float(clock.time(beat)), 2)  # noqa: E731
    tracks = []
    for mk, e in zip(tl.marks, entries):
        tr = e.track
        tracks.append({
            "index": mk["index"], "track_id": tr.id, "display_name": tr.display_name, "name": tr.name,
            "artists": list(tr.artists), "camelot": tr.camelot, "bpm": tr.bpm, "folder": e.folder,
            "start_s": t(mk["take_beat"]), "in_s": t(mk["in_beat"]), "out_s": t(mk["out_beat"]),
        })  # fmt: skip
    trans = [{**x, "at_s": t(x["beat"])} for x in tl.techniques]
    return {"duration_s": round(float(clock.time(tl.g_end)) + END_TAIL_S, 2), "tracks": tracks, "transitions": trans,
            "warnings": list(tl.warnings)}


# --- streaming pieces ----------------------------------------------------------------------------------------------


class _Acc:
    """Additive accumulator over global sample indices [base, ...)."""

    def __init__(self) -> None:
        self.base = 0
        self.buf = np.zeros((2, 0), np.float32)

    def add(self, start: int, x: np.ndarray) -> None:
        if x.shape[1] == 0:
            return
        if start < self.base:  # already flushed: should not happen, drop what's too late
            cut = self.base - start
            if cut >= x.shape[1]:
                return
            x, start = x[:, cut:], self.base
        end = start + x.shape[1]
        need = end - self.base
        if need > self.buf.shape[1]:
            grown = np.zeros((2, max(need, int(self.buf.shape[1] * 1.5) + SR)), np.float32)
            grown[:, : self.buf.shape[1]] = self.buf
            self.buf = grown
        self.buf[:, start - self.base:end - self.base] += x

    def pop(self, upto: int) -> np.ndarray:
        """Samples [base, upto) (zeros where nothing was added); they leave the accumulator."""
        n = max(0, upto - self.base)
        out = np.zeros((2, n), np.float32)
        k = min(n, self.buf.shape[1])
        out[:, :k] = self.buf[:, :k]
        self.buf = self.buf[:, k:].copy() if k < self.buf.shape[1] else np.zeros((2, 0), np.float32)
        self.base += n
        return out


class _Encoder:
    """Streams float PCM into ffmpeg: MP3 (libmp3lame) when available, else AAC in an m4a."""

    def __init__(self, out_path: Path, sr: int) -> None:
        ffmpeg = ffmpeg_path()
        if ffmpeg is None:
            raise ValueError("ffmpeg not found")
        self.codec = "libmp3lame" if _has_encoder(ffmpeg, "libmp3lame") else mixrender._aac_encoder(ffmpeg)
        self.out_path = out_path
        self._err = tempfile.TemporaryFile()
        fmt = ["-f", "mp3", "-id3v2_version", "3"] if self.codec == "libmp3lame" else ["-movflags", "+faststart",
                                                                                       "-f", "ipod"]
        cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "f32le", "-ar", str(sr),
               "-ac", "2", "-i", "-", "-c:a", self.codec, "-b:a", BITRATE, *fmt, str(out_path)]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._err)

    def write(self, x: np.ndarray) -> None:
        if x.shape[1] == 0:
            return
        try:
            self.proc.stdin.write(np.ascontiguousarray(np.clip(x, -1.0, 1.0).T.astype("<f4")).tobytes())
        except (BrokenPipeError, OSError) as e:
            raise ValueError(f"the encoder stopped: {self._stderr() or e}") from e

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            rc = self.proc.wait(timeout=ENCODE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise ValueError("the encoder did not finish") from None
        if rc != 0:
            raise ValueError(f"could not encode the mix: {self._stderr() or f'ffmpeg exit {rc}'}")

    def kill(self) -> None:
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    def _stderr(self) -> str:
        try:
            self._err.seek(0)
            return self._err.read().decode(errors="replace").strip().splitlines()[-1:][0]
        except (OSError, IndexError):
            return ""


_encoders: dict[tuple[str, str], bool] = {}


def _has_encoder(ffmpeg: str, name: str) -> bool:
    key = (ffmpeg, name)
    if key not in _encoders:
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20,
                                 stdin=subprocess.DEVNULL).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        _encoders[key] = f" {name} " in out
    return _encoders[key]


class _Bus:
    """Leveller + true-peak limiter over the streamed mix, block by block with context on both sides, then the
    encoder; also measures integrated loudness (100 ms K-weighted segments) and true peak."""

    def __init__(self, sr: int, enc: _Encoder, total: int) -> None:
        self.sr, self.enc, self.total = sr, enc, total
        self.ctx = int(BUS_CONTEXT_S * sr)
        self.block = int(BUS_BLOCK_S * sr)
        self.buf = np.zeros((2, 0), np.float32)
        self.buf0 = 0  # global index of buf[:, 0]
        self.done = 0  # emitted up to here
        self.ref = 10 ** ((mixrender.DECK_LUFS + 0.691) / 10)
        self.seg = int(0.1 * sr)
        self.kz = None
        self.k_rest = np.zeros((2, 0))
        self.seg_pow: list[float] = []
        self.peak = 0.0
        self.max_red = 0.0

    def feed(self, x: np.ndarray, final: bool = False) -> None:
        self.buf = np.concatenate([self.buf, x], axis=1)
        have = self.buf0 + self.buf.shape[1]
        while True:
            end = self.done + self.block
            if final:
                if self.done >= min(have, self.total):
                    break
                end = min(end, have, self.total)
            elif end + self.ctx > have:
                break
            self._process(self.done, end, have)

    def _process(self, a: int, b: int, have: int) -> None:
        lo, hi = max(self.buf0, a - self.ctx), min(have, b + self.ctx)
        seg = self.buf[:, lo - self.buf0:hi - self.buf0]
        gain, red = mixrender._level(seg, self.ref, self.sr)
        y, lim = mixrender._limit(seg * gain, mixrender.CEILING_DBTP)
        self.max_red = max(self.max_red, lim)
        out = np.ascontiguousarray(y[:, a - lo:b - lo])
        if a == 0:
            fi = min(out.shape[1], int(0.005 * self.sr))
            out[:, :fi] *= np.linspace(0, 1, fi, dtype=np.float32)
        if b >= self.total:
            fo = min(out.shape[1], int(0.03 * self.sr))
            out[:, out.shape[1] - fo:] *= np.linspace(1, 0, fo, dtype=np.float32)
        self._measure(out)
        self.enc.write(out)
        self.done = b
        keep = max(self.buf0, b - self.ctx)
        self.buf = self.buf[:, keep - self.buf0:]
        self.buf0 = keep

    def _measure(self, out: np.ndarray) -> None:
        sos = _k_sos(self.sr)
        if self.kz is None:
            self.kz = np.zeros((sos.shape[0], 2, 2))
        k, self.kz = signal.sosfilt(sos, out.astype(np.float64), axis=-1, zi=self.kz)
        k = np.concatenate([self.k_rest, k], axis=1)
        n = k.shape[1] // self.seg * self.seg
        if n:
            p = (k[:, :n] ** 2).sum(axis=0).reshape(-1, self.seg).mean(axis=1)
            self.seg_pow.extend(p.tolist())
        self.k_rest = k[:, n:]
        if out.shape[1]:
            up = signal.resample_poly(out.astype(np.float64), 4, 1, axis=-1)
            self.peak = max(self.peak, float(np.max(np.abs(up))))

    def lufs(self) -> float:
        p = np.asarray(self.seg_pow)
        if p.size < 4:
            return -70.0
        z = np.convolve(p, np.ones(4) / 4, mode="valid")  # 400 ms blocks, 75 % overlap
        z = z[-0.691 + 10 * np.log10(z + 1e-12) > -70]
        if not z.size:
            return -70.0
        rel = -0.691 + 10 * np.log10(z.mean()) - 10
        z = z[-0.691 + 10 * np.log10(z) > rel]
        return float(-0.691 + 10 * np.log10(z.mean())) if z.size else -70.0


_K_SOS: dict[int, np.ndarray] = {}


def _k_sos(sr: int) -> np.ndarray:
    """BS.1770 K-weighting as sos (same filter as mixrender._k_weight)."""
    if sr not in _K_SOS:
        a_ = 10 ** (4.0 / 40)
        w0 = 2 * np.pi * 1500.0 / sr
        al = np.sin(w0) / (2 * (1 / np.sqrt(2)))
        c, sa = np.cos(w0), 2 * np.sqrt(a_) * al
        shelf = [a_ * ((a_ + 1) + (a_ - 1) * c + sa), -2 * a_ * ((a_ - 1) + (a_ + 1) * c),
                 a_ * ((a_ + 1) + (a_ - 1) * c - sa), (a_ + 1) - (a_ - 1) * c + sa, 2 * ((a_ - 1) - (a_ + 1) * c),
                 (a_ + 1) - (a_ - 1) * c - sa]
        w0 = 2 * np.pi * 38.0 / sr
        al, c = np.sin(w0) / (2 * 0.5), np.cos(w0)
        hp = [(1 + c) / 2, -(1 + c), (1 + c) / 2, 1 + al, -2 * c, 1 - al]
        _K_SOS[sr] = np.array([np.r_[np.array(s[:3]) / s[3], 1.0, np.array(s[4:]) / s[3]] for s in (shelf, hp)])
    return _K_SOS[sr]


class _Iso:
    """mixrender's 3-band isolator with filter state carried from chunk to chunk."""

    def __init__(self, sr: int) -> None:
        lp1 = signal.butter(2, mixrender.XOVER_LOW_HZ, "lowpass", fs=sr, output="sos")
        hp1 = signal.butter(2, mixrender.XOVER_LOW_HZ, "highpass", fs=sr, output="sos")
        lp2 = signal.butter(2, mixrender.XOVER_HIGH_HZ, "lowpass", fs=sr, output="sos")
        hp2 = signal.butter(2, mixrender.XOVER_HIGH_HZ, "highpass", fs=sr, output="sos")
        self.sos = {"low": np.vstack([lp1, lp1, mixrender._allpass_sos(mixrender.XOVER_HIGH_HZ, sr)]),
                    "rest": np.vstack([hp1, hp1]), "mid": np.vstack([lp2, lp2]), "high": np.vstack([hp2, hp2])}
        self.zi = {k: np.zeros((v.shape[0], 2, 2)) for k, v in self.sos.items()}

    def __call__(self, x: np.ndarray, gl: np.ndarray, gm: np.ndarray, gh: np.ndarray) -> np.ndarray:
        xd = x.astype(np.float64)
        low, self.zi["low"] = signal.sosfilt(self.sos["low"], xd, axis=-1, zi=self.zi["low"])
        rest, self.zi["rest"] = signal.sosfilt(self.sos["rest"], xd, axis=-1, zi=self.zi["rest"])
        mid, self.zi["mid"] = signal.sosfilt(self.sos["mid"], rest, axis=-1, zi=self.zi["mid"])
        high, self.zi["high"] = signal.sosfilt(self.sos["high"], rest, axis=-1, zi=self.zi["high"])
        return (low * gl + mid * gm + high * gh).astype(np.float32)


# --- deck rendering -------------------------------------------------------------------------------------------------


def _chunk_edges(d: _Deck) -> list[float]:
    """Chunk edges (global beats) every ~CHUNK_BEATS, moved clear of the deck's automation spans."""
    spans = [(s - SPAN_MARGIN, t + SPAN_MARGIN) for s, t in d.spans]

    def busy(b: float) -> float | None:
        for s, t in spans:
            if s <= b <= t:
                return t
        return None

    edges = [d.p0]
    cur = d.p0
    while True:
        nxt = math.floor(cur + CHUNK_BEATS)
        while (t := busy(nxt)) is not None:
            nxt = math.ceil(t) + 1
        if nxt >= d.p1 - 16:
            break
        edges.append(float(nxt))
        cur = nxt
    edges.append(d.p1)
    return edges


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    idx = np.flatnonzero(mask)
    if not idx.size:
        return []
    cuts = np.flatnonzero(np.diff(idx) > 1)
    starts = np.r_[idx[0], idx[cuts + 1]]
    ends = np.r_[idx[cuts], idx[-1]] + 1
    return list(zip(starts.tolist(), ends.tolist()))


def _stretch_chunk(audio: np.ndarray, d: _Deck, clock: mixrender._Clock, edges: list[float], k: int, sr: int,
                   key_lock: bool) -> tuple[int, int, np.ndarray]:
    """Chunk k of a deck stretched onto the master clock: (first sample, end sample, audio). Every chunk but the
    first starts XFADE samples before its beat line (spliced onto the previous chunk there)."""
    deck = d.deck
    nbpm = float(deck["native_bpm"])
    spb = 60.0 / nbpm
    b0, b1 = edges[k], edges[k + 1]
    first, last = k == 0, k == len(edges) - 2
    t_p0, t_p1 = float(clock.time(d.p0)) * sr, float(clock.time(d.p1)) * sr
    q_start = max(0, int(math.floor(t_p0)) - int(round(0.003 * sr))) if first \
        else int(round(float(clock.time(b0)) * sr)) - XFADE
    q_end = int(math.ceil(t_p1)) + int(0.25 * sr) if last else int(round(float(clock.time(b1)) * sr))
    s0, s1 = b0 - 2.0, b1 + 1.0 + (0.5 if last else 0.0)
    src, _ = mixrender._source(audio, deck, s0, s1, sr)
    beats = s0 + np.arange(src.shape[1]) / (sr * spb)
    edge = 2 * mixrender._Clock.STEP
    speed = np.clip(clock.bpm(np.clip(beats, d.p0 + edge, d.p1 - edge)) / nbpm, 0.5, 2.0).astype(np.float64)
    q = np.arange(q_start, q_end)
    jq = (clock.beat(q / sr) - s0) * sr * spb
    raw, _note = mixrender._to_master(src, speed, jq, sr, key_lock)
    return q_start, q_end, raw


def _render_deck(d: _Deck, audio: np.ndarray, trim: float, clock: mixrender._Clock, sr: int, acc: _Acc,
                 pool: ThreadPoolExecutor | None = None, key_lock: bool = True,
                 cancel: threading.Event | None = None) -> Iterator[int]:
    """Render one deck chunk by chunk into `acc` (dry signal + its echo / reverb returns), the stretches running
    ahead on `pool`. Yields the global sample up to which this deck's dry signal is complete after each chunk."""
    deck = d.deck
    t_p0, t_p1 = float(clock.time(d.p0)) * sr, float(clock.time(d.p1)) * sr
    fade_in, fade_out = int(round(0.003 * sr)), int(round(0.004 * sr))
    lanes = deck["lanes"]
    eq_used = any(k["value"] != 0 for lane in ("eq_low", "eq_mid", "eq_high") for k in lanes[lane])
    iso = _Iso(sr) if eq_used else None
    edges = _chunk_edges(d)
    n_chunks = len(edges) - 1
    ahead: deque[Future] = deque()
    nxt = 0

    def submit() -> None:
        nonlocal nxt
        while nxt < n_chunks and len(ahead) < (STRETCH_WORKERS + 1 if pool else 1):
            args = (audio, d, clock, edges, nxt, sr, key_lock)
            if pool is not None:
                ahead.append(pool.submit(_stretch_chunk, *args))
            else:
                f: Future = Future()
                f.set_result(_stretch_chunk(*args))
                ahead.append(f)
            nxt += 1

    pending: np.ndarray | None = None  # previous chunk's last XFADE samples (to splice with)
    fin = np.sin(0.5 * np.pi * (np.arange(XFADE) + 0.5) / XFADE).astype(np.float32)
    fout = np.cos(0.5 * np.pi * (np.arange(XFADE) + 0.5) / XFADE).astype(np.float32)
    try:
        for k in range(n_chunks):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            submit()
            q_start, q_end, raw = ahead.popleft().result()
            submit()
            last = k == n_chunks - 1
            if k and pending is not None:  # [q_start, q_start + XFADE) is shared with the previous chunk
                raw[:, :XFADE] = pending * fout + raw[:, :XFADE] * fin
            if not last:
                pending = raw[:, -XFADE:].copy()
                x, seg_q0, seg_q1 = raw[:, :-XFADE], q_start, q_end - XFADE
            else:
                x, seg_q0, seg_q1 = raw, q_start, q_end
            n = x.shape[1]
            qq = np.arange(seg_q0, seg_q1, dtype=np.float64)
            g = np.minimum(np.clip((qq - (t_p0 - fade_in)) / max(fade_in, 1), 0, 1),
                           np.clip((t_p1 - qq) / max(fade_out, 1), 0, 1)).astype(np.float32)  # the play range
            x = x * ((np.sin(0.5 * np.pi * g) ** 2) * trim).astype(np.float32)
            if deck.get("fx"):
                x = mixrender._apply_fx(x, deck["fx"], clock, seg_q0, sr)
            ctl_beats = clock.beat((seg_q0 + np.arange(0, n + mixrender.CTL, mixrender.CTL)) / sr)

            def lane(name: str, smooth: float = mixrender.STEP_RAMP_S) -> np.ndarray:
                return mixrender._ctl(lanes[name], ctl_beats, smooth, sr, n)

            if any(kf["value"] != 0 for kf in lanes["filter"]):
                f = lane("filter", mixrender.FILTER_SMOOTH_S)
                for r0, r1 in _runs(np.abs(f) > 1e-4):  # dry outside: filter only where it's open
                    a, b = max(0, r0 - int(0.05 * sr)), min(n, r1 + int(0.05 * sr))
                    x[:, a:b] = mixrender._dj_filter(x[:, a:b], f[a:b], sr)
            for kind in ("echo_send", "reverb_send"):
                if not any(kf["value"] > 0 for kf in lanes[kind]):
                    continue
                send = lane(kind)
                for r0, r1 in _runs(send > 1e-4):
                    _send_return(kind, x[:, r0:r1] * send[r0:r1], seg_q0 + r0, deck, clock, sr, acc)
            if iso is not None:
                x = iso(x, *(mixrender._iso_gain(lane(nm)) for nm in ("eq_low", "eq_mid", "eq_high")))
            acc.add(seg_q0, x * lane("volume"))
            yield seg_q1
    finally:
        for f in ahead:
            f.cancel()


def _send_return(kind: str, sig: np.ndarray, q0: int, deck: dict, clock: mixrender._Clock, sr: int,
                 acc: _Acc) -> None:
    """One send region through its effect, tail included, added into the mix."""
    if kind == "reverb_send":
        tail = int(REVERB_TAIL_S * sr)
        rin = np.zeros((2, sig.shape[1] + tail), np.float32)
        rin[:, : sig.shape[1]] = sig
        acc.add(q0, mixrender.REVERB_RETURN * mixrender._reverb(rin, sr))
        return
    beats = clock.beat(np.arange(q0, q0 + sig.shape[1]) / sr)
    fb_kf = deck["lanes"]["echo_feedback"]
    fb_max = max(k["value"] for k in fb_kf)
    d_s = float(deck["echo_beats"]) * 60.0 / float(np.median(clock.bpm(beats)))
    tail_s = min(ECHO_TAIL_MAX_S, d_s * math.log(1e-3) / math.log(max(0.05, min(0.95, fb_max))) + 1.0)
    n = sig.shape[1] + int(tail_s * sr)
    ein = np.zeros((2, n), np.float32)
    ein[:, : sig.shape[1]] = sig
    all_beats = clock.beat(np.arange(q0, q0 + n) / sr)
    delay = float(deck["echo_beats"]) * 60.0 / clock.bpm(all_beats) * sr
    fb = np.asarray(mixplan.lane_value(fb_kf, all_beats), dtype=np.float32)
    acc.add(q0, mixrender.ECHO_RETURN * mixrender._echo(ein, delay, fb, sr))


# --- public API ----------------------------------------------------------------------------------------------------


def render(entries: list[SetEntry], plans: list[dict], out_path: Path, *, title: str = "", sr: int = SR,
           progress: Callable[[float, str], None] | None = None, cancel: threading.Event | None = None) -> dict:
    """Render the whole set to `out_path` (MP3 320 kbps; .m4a when ffmpeg has no MP3 encoder: the returned
    `format` says which). Returns summary() + lufs, true_peak_db, format, warnings. ValueError for an unusable set,
    Cancelled when `cancel` is set."""
    tl = build(entries, plans)
    info = summary(tl, entries)
    clock = mixrender._Clock(tl.tempo, tl.g_start - 4, tl.g_end + 4, tl.g_start)
    total = int(round((float(clock.time(tl.g_end)) + END_TAIL_S) * sr))
    if total > 4 * 3600 * sr:
        raise ValueError("the set is longer than 4 hours")
    work = [max(1.0, float(clock.time(d.p1) - clock.time(d.p0))) for d in tl.decks]
    done_work = 0.0

    def report(f: float, msg: str) -> None:
        if progress is not None:
            try:
                progress(max(0.0, min(1.0, f)), msg)
            except Exception:  # noqa: BLE001 - a progress sink never breaks a render
                pass

    out_path = Path(out_path)
    enc = _Encoder(out_path, sr)
    bus = _Bus(sr, enc, total)
    acc = _Acc()
    warnings = list(tl.warnings)
    pool = ThreadPoolExecutor(STRETCH_WORKERS, thread_name_prefix="setmix-stretch")
    loader = ThreadPoolExecutor(1, thread_name_prefix="setmix-decode")
    decoded = loader.submit(mixrender.decode_stereo, tl.decks[0].entry.path, sr)
    try:
        report(0.01, "Starting")
        for di, d in enumerate(tl.decks):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            name = d.entry.track.display_name
            report(done_work / sum(work), f"Track {di + 1}/{len(tl.decks)} · {name}")
            audio = decoded.result()
            if di + 1 < len(tl.decks):  # decode the next track while this one renders
                decoded = loader.submit(mixrender.decode_stereo, tl.decks[di + 1].entry.path, sr)
            lufs = mixrender._track_lufs(d.entry.path, audio, sr)
            trim = 10 ** (mixrender._clamp(mixrender.DECK_LUFS - lufs, -mixrender.TRIM_LIMIT_DB,
                                           mixrender.TRIM_LIMIT_DB) / 20)
            nxt = tl.decks[di + 1] if di + 1 < len(tl.decks) else None
            next_q = int(math.floor(float(clock.time(nxt.p0)) * sr)) - SR if nxt else total
            t0 = float(clock.time(d.p0)) * sr
            t1 = float(clock.time(d.p1)) * sr
            for upto in _render_deck(d, audio, trim, clock, sr, acc, pool=pool, cancel=cancel):
                safe = min(upto, next_q) - int(0.5 * sr)
                if safe > acc.base:
                    bus.feed(acc.pop(safe))
                frac = (min(upto, t1) - t0) / max(1.0, t1 - t0)
                report((done_work + work[di] * frac) / sum(work), f"Track {di + 1}/{len(tl.decks)} · {name}")
            done_work += work[di]
            del audio
        report(0.98, "Finishing")
        bus.feed(acc.pop(total), final=True)
        enc.close()
    except BaseException:
        enc.kill()
        Path(out_path).unlink(missing_ok=True)
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        loader.shutdown(wait=False, cancel_futures=True)
    if bus.max_red > 8:
        warnings.append(f"limiter worked hard in places ({bus.max_red:.1f} dB)")
    fmt = "mp3" if enc.codec == "libmp3lame" else "m4a"
    if fmt == "mp3":
        try:
            _tag(out_path, title or "Seedbox mix", info, entries)
        except Exception as e:  # noqa: BLE001 - tags are a nicety
            log.warning("couldn't tag %s: %s", out_path.name, e)
    report(1.0, "Done")
    return {**info, "duration_s": round(total / sr, 2), "lufs": round(bus.lufs(), 2),
            "true_peak_db": round(20 * math.log10(max(bus.peak, 1e-9)), 2), "format": fmt, "warnings": warnings}


def _ts(t: float, frames: bool = False) -> str:
    t = max(0.0, t)
    m, s = divmod(t, 60)
    if frames:  # cue sheets: mm:ss:ff at 75 frames per second
        return f"{int(m):02d}:{int(s):02d}:{int(round((s - int(s)) * 75)) % 75:02d}"
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{int(s):02d}" if h else f"{m}:{int(s):02d}"


def cue_sheet(result: dict, title: str, audio_name: str) -> str:
    """A .cue sheet for the mix file (INDEX 01 = where each track takes over)."""
    q = lambda s: str(s).replace('"', "'")  # noqa: E731
    lines = ['PERFORMER "Seedbox"', f'TITLE "{q(title)}"',
             f'FILE "{q(audio_name)}" {"MP3" if audio_name.lower().endswith(".mp3") else "WAVE"}']
    for n, tr in enumerate(result["tracks"], 1):
        lines += [f"  TRACK {n:02d} AUDIO", f'    TITLE "{q(tr["name"])}"',
                  f'    PERFORMER "{q(", ".join(tr["artists"]))}"', f"    INDEX 01 {_ts(tr['start_s'], frames=True)}"]
    return "\n".join(lines) + "\n"


def tracklist(result: dict, title: str) -> str:
    """Plain-text tracklist with timestamps, keys, tempos and how each transition is mixed."""
    lines = [title, f"{len(result['tracks'])} tracks · {_ts(result['duration_s'])}", ""]
    trans = {t["index"]: t for t in result.get("transitions", [])}
    for n, tr in enumerate(result["tracks"], 1):
        how = ""
        if n > 1 and (t := trans.get(n - 2)):
            how = f"   ← {t['technique']}" + (f" ({t['overlap']} bars)" if t.get("overlap") else "")
        lines.append(f"{_ts(tr['start_s']):>8}  {n:2d}. {tr['display_name']}  [{tr['camelot'] or '?'} · "
                     f"{round(tr['bpm']) if tr['bpm'] else '?'} BPM]{how}")
    lines += ["", "Mixed with Seedbox."]
    return "\n".join(lines) + "\n"


def _tag(path: Path, title: str, info: dict, entries: list[SetEntry]) -> None:
    """Title / artist / tracklist comment, ID3 chapters per track and the first track's cover art."""
    from mutagen.id3 import APIC, CHAP, COMM, CTOC, ID3, TALB, TIT2, TPE1, TPE2, CTOCFlags
    from mutagen.id3 import ID3NoHeaderError

    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.delall("TIT2")
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text="Seedbox"))
    tags.add(TPE2(encoding=3, text="Seedbox"))
    tags.add(TALB(encoding=3, text=title))
    tags.add(COMM(encoding=3, lang="eng", desc="Tracklist", text=tracklist(info, title)))
    total_ms = int(info["duration_s"] * 1000)
    ids = []
    tracks = info["tracks"]
    for n, tr in enumerate(tracks):
        cid = f"chp{n}"
        ids.append(cid)
        start = int(tr["start_s"] * 1000)
        end = int(tracks[n + 1]["start_s"] * 1000) if n + 1 < len(tracks) else total_ms
        tags.add(CHAP(element_id=cid, start_time=start, end_time=max(start + 1, end),
                      sub_frames=[TIT2(encoding=3, text=tr["display_name"])]))
    tags.add(CTOC(element_id="toc", flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED, child_element_ids=ids,
                  sub_frames=[TIT2(encoding=3, text="Tracklist")]))
    for e in entries:
        try:
            art = [f for f in ID3(e.path).getall("APIC") if f.data]
        except Exception:  # noqa: BLE001
            art = []
        if art:
            a = next((f for f in art if f.type == 3), art[0])
            tags.add(APIC(encoding=3, mime=a.mime or "image/jpeg", type=3, desc="Cover", data=a.data))
            break
    tags.save(path, v2_version=3)


def write_sidecars(result: dict, title: str, audio_path: Path) -> list[Path]:
    """<mix>.cue and <mix>.txt next to the audio file (atomic)."""
    out = []
    for suffix, text in ((".cue", cue_sheet(result, title, audio_path.name)), (".txt", tracklist(result, title))):
        p = audio_path.with_suffix(suffix)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.stem}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        out.append(p)
    return out
