"""Time stretch and pitch shift for a deck: Signalsmith Stretch, driven by a time map worked out from the plan (#52).

Seedbox 1 runs Rubber Band twice, once on the music and once on a click track to find out where the music went. Here
nothing is measured. `time_map` gives each output sample its source position from the plan alone: the tempo lane,
integrated, says which master beat a sample is on, and `plan.deck_time` says where in the file that beat is.

Stretching follows the map by construction. The engine runs at a constant ratio for each `Piece`, and a piece's first
sample goes exactly where the map says, so a piece can only be off by how far the map bends away from a straight line
within it. `pieces` keeps that under `TOLERANCE_S`, in as few pieces as it can, cut on master beats where the kick hides
the seam. A deck at a constant ratio is one piece, with no seam.

Why pieces: `python_stretch.process` seeks and flushes on every call, so the ratio can't change inside one run. Each
piece gets its own run, with `block` samples of music before it so that it has settled by the cut. A seam crossfades
over `SEAM_S`, ending on the cut, which leaves the attack of the beat it lands on untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import python_stretch

from .plan import Deck, MixPlan, deck_grid, deck_position, lane_value

TOLERANCE_S = 0.0005  # how far a piece may be from the map
SEAM_S = 0.02
_MAP_STEP_BEATS = 1 / 64  # the tempo lane is integrated over steps this long, plus its own keyframes
_SAME_RATIO = 1e-6  # closer to 1 than this over a piece, it is copied rather than stretched
_JUMP_SAMPLES = 2.0  # a source step this far from 1 ratio's worth is a jump: a loop, or a gap in the grid
_TONALITY_HZ = 8000.0  # above this a shift doesn't keep partials' harmonic relations (Signalsmith's own default)


@dataclass(frozen=True)
class Profile:
    """How the engine listens: longer blocks resolve pitch, shorter ones resolve time."""

    block_s: float
    interval_s: float


FULL = Profile(0.12, 0.03)  # Signalsmith's default preset
DRUMS = Profile(0.04, 0.01)  # transients over tone: pre-echo can't reach further back than a block
STEM_PROFILES = {"drums": DRUMS, "bass": FULL, "vocals": FULL, "other": FULL}


@dataclass(frozen=True)
class Piece:
    """Output samples [start, end) read from `source` (fractional samples) at `ratio` source samples per output
    sample."""

    start: int
    end: int
    source: float
    ratio: float


def master_beats(tempo: list, start_beat: float, end_beat: float, sr: int) -> np.ndarray:
    """The master beat at each output sample from `start_beat` up to `end_beat`: 60/tempo integrated along the lane."""
    if end_beat <= start_beat:
        return np.empty(0)
    steps = int(np.ceil((end_beat - start_beat) / _MAP_STEP_BEATS))
    inside = [k.beat for k in tempo if start_beat < k.beat < end_beat]
    knots = np.unique(np.concatenate([np.linspace(start_beat, end_beat, steps + 1), inside]))
    # the midpoint of a step is never on a keyframe, so a tempo step falls cleanly between two steps
    bpm = np.asarray(lane_value(tempo, (knots[:-1] + knots[1:]) / 2), dtype=float)
    seconds = np.concatenate([[0.0], np.cumsum(np.diff(knots) * 60.0 / bpm)])
    n = int(np.floor(seconds[-1] * sr))
    return np.interp(np.arange(n) / sr, seconds, knots)


def time_map(deck: Deck, beats: np.ndarray, sr: int) -> np.ndarray:
    """The source position, in fractional samples, of each output sample at these master beats."""
    return deck_grid(deck).time_at(deck_position(deck, beats)) * sr


def _fits(pos: np.ndarray, i: int, j: int, tol: float) -> bool:
    """Does a straight line through pos[i] and pos[j] stay within tol of the map?"""
    idx = np.linspace(i, j - 1, min(j - i, 512)).astype(np.int64)
    ratio = (pos[j - 1] - pos[i]) / max(j - 1 - i, 1)
    return bool(np.abs(pos[idx] - (pos[i] + (idx - i) * ratio)).max() <= tol)


def pieces(pos: np.ndarray, beats: np.ndarray, sr: int, tolerance_s: float = TOLERANCE_S) -> list[Piece]:
    """Cut the map into constant-ratio pieces: at every jump, and at the fewest master beats that keep it in tolerance
    (at quarter beats if a tempo moves so fast that whole beats don't)."""
    if pos.size == 0:
        return []
    steps = np.diff(pos)
    typical = float(np.median(steps)) if steps.size else 1.0
    jumps = np.flatnonzero(np.abs(steps - typical) > _JUMP_SAMPLES + abs(typical)) + 1
    whole = np.flatnonzero(np.diff(np.floor(beats + 1e-9))) + 1
    quarter = np.flatnonzero(np.diff(np.floor(beats * 4 + 1e-9))) + 1
    tol = tolerance_s * sr
    out: list[Piece] = []
    for run0, run1 in zip([0, *jumps.tolist()], [*jumps.tolist(), pos.size]):
        i = run0
        while i < run1:
            j = _farthest(pos, i, run1, whole, tol) or _farthest(pos, i, run1, quarter, tol) or _next(quarter, i, run1)
            out.append(_piece(pos, i, j))
            i = j
    return out


def _farthest(pos: np.ndarray, i: int, run1: int, cuts: np.ndarray, tol: float) -> int | None:
    """The last of (cuts within the run, the run's end) that a piece from i reaches in tolerance. None: not even one."""
    if _fits(pos, i, run1, tol):
        return run1
    ends = cuts[(cuts > i) & (cuts < run1)]
    lo, hi = 0, ends.size  # the map bends one way at a time, so what fits is a prefix
    while lo < hi:
        mid = (lo + hi) // 2
        if _fits(pos, i, int(ends[mid]), tol):
            lo = mid + 1
        else:
            hi = mid
    return int(ends[lo - 1]) if lo else None


def _next(cuts: np.ndarray, i: int, run1: int) -> int:
    ends = cuts[(cuts > i) & (cuts < run1)]
    return int(ends[0]) if ends.size else run1


def _piece(pos: np.ndarray, i: int, j: int) -> Piece:
    ratio = float(pos[j - 1] - pos[i]) / (j - 1 - i) if j - 1 > i else 1.0
    return Piece(i, j, float(pos[i]), ratio)


def _slice(src: np.ndarray, a: int, b: int) -> np.ndarray:
    """src[:, a:b], with silence outside the file."""
    out = np.zeros((src.shape[0], b - a), np.float32)
    i, j = max(a, 0), min(b, src.shape[1])
    if i < j:
        out[:, i - a : j - a] = src[:, i:j]
    return out


class _Engine:
    def __init__(self, channels: int, sr: int, semitones: float, profile: Profile) -> None:
        self.block = round(profile.block_s * sr)
        self.semitones = semitones
        self.engine = python_stretch.Signalsmith.Stretch()
        self.engine.configure(channels, self.block, round(profile.interval_s * sr))
        self.tonality = _TONALITY_HZ / sr

    def run(self, x: np.ndarray, ratio: float) -> np.ndarray:
        # setTransposeSemitones is lost when process() resets the engine at its end
        self.engine.setTransposeSemitones(self.semitones, self.tonality)
        self.engine.setTimeFactor(ratio)
        return self.engine.process(x)


def _render_piece(p: Piece, src: np.ndarray, engine: _Engine, lead: int, memo: dict) -> np.ndarray:
    """`lead` samples before the piece's start, then the piece."""
    n = p.end - p.start
    key = (round(p.source, 2), round(p.ratio, 8), n, lead)
    if key in memo:  # a loop plays the same slice again and again
        return memo[key]
    if abs(p.ratio - 1.0) < _SAME_RATIO and not engine.semitones:
        a = round(p.source) - lead
        out = _slice(src, a, a + lead + n)
    else:
        pre = engine.block + int(np.ceil(lead * p.ratio))
        a = int(np.floor(p.source)) - pre
        b = int(np.ceil(p.source + n * p.ratio)) + engine.block
        x = _slice(src, a, b)
        if not x.any():
            out = np.zeros((src.shape[0], lead + n), np.float32)
        else:
            y = engine.run(x, p.ratio)
            first = round((p.source - a) / p.ratio) - lead
            out = np.zeros((src.shape[0], lead + n), np.float32)
            got = y[:, first : first + lead + n]
            out[:, : got.shape[1]] = got
    memo[key] = out
    return out


def stretch(src: np.ndarray, pos: np.ndarray, beats: np.ndarray, sr: int, *, semitones: float = 0.0,
            profile: Profile = FULL) -> np.ndarray:  # fmt: skip
    """`src` (channels, samples) played along the map: output sample i is source position pos[i], at the source's pitch
    plus `semitones`. `beats` (one per output sample) says where seams may go."""
    plan = pieces(pos, beats, sr)
    out = np.zeros((src.shape[0], pos.size), np.float32)
    if not plan:
        return out
    engine = _Engine(src.shape[0], sr, semitones, profile)
    seam = round(SEAM_S * sr)
    fade = (np.arange(seam) + 0.5) / max(seam, 1) * np.pi / 2
    fade_in, fade_out = np.sin(fade).astype(np.float32), np.cos(fade).astype(np.float32)
    memo: dict = {}
    for k, p in enumerate(plan):
        lead = seam if k else 0
        y = _render_piece(p, src, engine, lead, memo)
        # A piece shorter than the seam can't take a whole crossfade: the part before its predecessor would be lost.
        tail = min(lead, plan[k - 1].end - plan[k - 1].start) if k else 0
        if tail:
            t0 = seam - tail
            out[:, p.start - tail : p.start] = (
                out[:, p.start - tail : p.start] * fade_out[t0:] + y[:, t0:seam] * fade_in[t0:]
            )
        out[:, p.start : p.end] = y[:, lead:]
    return out


def _resample(x: np.ndarray, pos: np.ndarray) -> np.ndarray:
    from musicreco.mixrender import _resample_at

    return _resample_at(x, pos)


def _window(plan: MixPlan, deck: Deck, sr: int) -> tuple[np.ndarray, np.ndarray, int, int]:
    """The master beats of the window, and the part of them this deck plays: (all beats, source map, first, last)."""
    w = plan.window
    beats = master_beats(plan.tempo, w.start_beat, w.end_beat, sr)
    i0, i1 = np.searchsorted(beats, [deck.play_from_beat, deck.play_to_beat])
    return beats, time_map(deck, beats[i0:i1], sr), int(i0), int(i1)


def render_deck(plan: MixPlan, name: str, audio: np.ndarray, sr: int, *, profile: Profile = FULL) -> np.ndarray:
    """A deck's audio on the master timeline, one sample per sample of the plan's window, silent where the deck doesn't
    play. With key lock the pitch stays at the source's plus `key_shift`; without it the pitch also follows the tempo,
    as on a turntable."""
    deck: Deck = getattr(plan, name)
    beats, pos, i0, i1 = _window(plan, deck, sr)
    out = np.zeros((audio.shape[0], beats.size), np.float32)
    if i1 <= i0:
        return out
    if plan.key_lock:
        out[:, i0:i1] = stretch(audio, pos, beats[i0:i1], sr, semitones=deck.key_shift, profile=profile)
        return out
    if deck.key_shift:
        # shift first, at the file's own speed: only the part that plays
        a, b = int(pos.min()) - 1, int(pos.max()) + 2
        here = np.arange(a, b, dtype=float)
        shifted = stretch(audio, here, here / sr, sr, semitones=deck.key_shift, profile=profile)
        out[:, i0:i1] = _resample(shifted, pos - a)
    else:
        out[:, i0:i1] = _resample(audio, pos)
    return out


def render_stems(plan: MixPlan, name: str, stems: Mapping[str, np.ndarray], sr: int) -> dict[str, np.ndarray]:
    """Each of a deck's stems on the master timeline, each with its own profile: the drums keep their transients."""
    return {stem: render_deck(plan, name, y, sr, profile=STEM_PROFILES.get(stem, FULL)) for stem, y in stems.items()}
