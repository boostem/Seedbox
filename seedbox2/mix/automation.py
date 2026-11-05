"""Automation shaped by the audio (#56): a generated plan's lanes, set from what the two tracks actually sound like.

Seedbox 1's lanes are the same constants for every pair. `adapt` listens to both decks through the plan, on its master
timeline (so loops and tempo changes are in it), and works in this order:

1. Filter ranges: every cutoff in a filter lane moves by the track's spectral centroid against a typical track's, so a
   dark track isn't gutted by the sweep that a bright one barely notices.
2. Sends: the outgoing deck's echo time, feedback, send levels and reverb size from the tempo and how busy its last
   phrase is. Busy phrases get quieter, smaller effects: their repeats would pile up.
3. Masking: bar by bar and band by band, how much of the two decks' energy is common to both (the index the mix metrics
   report). Mids and highs of the deck that isn't leading are cut only as far as it takes to bring the index down to
   TARGET_MASKING, and not at all where the tracks don't collide. With stems, a band that is mostly one stem has that
   stem turned down instead. The bass hand-off stays as planned (which bassline plays is the technique's call, #53),
   except that a kill opens where the other deck has no lows to clash with, and a hole in the lows at the hand-off closes.
4. Gain riding: the predicted short-term loudness of the sum against the line between the tracks' levels, corrected bar
   by bar within RIDE_BOUNDS: down on the faders, up on the EQ bands that aren't killed.

Everything it writes is ordinary keyframes, so the result is the default the user can override. A plan marked `edited`
comes back as it is, and lanes and send settings a person changed (a deck's `pinned`) are left alone, with the rest of
the plan fitted around them; `with_pins` carries them onto a freshly generated plan.

It predicts from band levels rather than rendering, so it is quick (well under a second per transition, against
seconds for a render) and works the same for the offline renderer and the live engine. An offline render can do better:
`ride_measured` corrects the plan from the loudness measured on a render (`offline.render_adapted` does this).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage

from .plan import LANE_RANGES, STEM_LANES, Deck, Keyframe, Lanes, MixPlan, Sends, StemLanes, deck_time, lane_value

GRID_PER_BEAT = 8
BEATS_PER_BAR = 4
FFT, HOP = 4096, 2048
SUBBANDS = np.geomspace(20.0, 20000.0, 33)
CENTRES = np.sqrt(SUBBANDS[:-1] * SUBBANDS[1:])

# The renderers' isolator and filter (musicreco/mixrender.py), which the prediction has to match.
XOVER_LOW_HZ, XOVER_HIGH_HZ = 200.0, 2500.0
FILTER_LP_HZ = (80.0, 20000.0)
FILTER_HP_HZ = (20.0, 10000.0)
EQ_BANDS = ("eq_low", "eq_mid", "eq_high")
CUT_BANDS = ("eq_mid", "eq_high")
KILL_LANE_DB = -30.0
LOW_GONE_FADER = 0.25  # A's lows have left once its fader is this far down
MIN_LOW_GAP_BEATS, MAX_LOW_GAP_BEATS = 1.0, 16.0
LOW_FREE_DB = 20.0  # the leading deck's lows this far under the other's: no clash, the other's kill can open
MIN_FREE_BARS = 2
KILL_DB = -24.0  # at or below this a band counts as killed: the isolator fades it to silence by -30
PLANNED_DUCK_DB = -18.0  # a generator's duck under a vocal clash: kept, the two lines can't be untangled by level

TARGET_MASKING = 0.25  # about 8.5 dB between the decks in a band
MAX_CUT_DB = 18.0
CUT_STEP_DB = 1.0
QUIET_BAND_DB = -40.0  # a band this far under the bar's total isn't worth cutting
STEM_SHARE = 0.6  # a stem with this share of a band takes the cut itself
MAX_STEM_CUT_DB = 18.0

SHORT_TERM_S = 3.0
REF_S = 4.0
RIDE_DEAD_LU = 0.5  # what riding leaves alone, inside the ticket's +-1 LU
RIDE_BOUNDS = (-9.0, 6.0)  # dB
RIDE_ITERATIONS = 6
MEASURED_TRIGGER_LU, MEASURED_GAIN = 0.7, 0.7  # correcting from a render: a bar's worst moment, not its mean
RIDE_GAIN = 0.8
DECK_LUFS, LEVEL_THRESHOLD_DB, LEVEL_RATIO, MAKEUP_DB = -12.0, 1.0, 3.0, 1.0  # the renderer's bus leveller

REF_CENTROID_HZ = 270.0  # the median over the golden corpus: at this the cutoffs are as the generator set them
CENTROID_RATIO = (0.5, 2.0)

OUT_PHRASE_BEATS = 32.0
ECHO_DIVISIONS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0)  # beats: stays on the grid
ECHO_MS = (480.0, 300.0)  # the delay to aim for on a sparse and on a busy phrase
SEND_SCALE = (1.15, 0.7)
FEEDBACK_SCALE = (1.1, 0.8)
REVERB_SIZE = (0.85, 0.55)
DENSITY_ONSETS = (1.0, 3.0)  # onsets per beat that count as sparse and as busy

_LEFT = 1e-6
_LANE_STEP = 1.0  # beats between the samples of a rewritten lane, before the straight runs are merged
_SAME = {"volume": 0.004, "gain": 0.004, "db": 0.15}


@dataclass(frozen=True)
class DeckAudio:
    """A deck's audio, (channels, n) at `sr`. `trim_db` is what the renderer will trim it by; `stems` by name, as in
    `analysis.stems`, when the track has them."""

    mix: np.ndarray
    sr: int
    trim_db: float = 0.0
    stems: dict[str, np.ndarray] | None = None


@dataclass
class Adapted:
    plan: MixPlan
    report: dict = field(default_factory=dict)


# --- what the decks sound like on the master timeline ----------------------------------------------------------------


def _k_weight_power() -> np.ndarray:
    """BS.1770's K-weighting as a power gain per sub-band: the high shelf and the low cut."""
    f = CENTRES
    shelf = 1.0 + (10 ** (4.0 / 10) - 1.0) * f**2 / (f**2 + 1500.0**2)
    cut = f**4 / (f**4 + 38.0**4)
    return shelf * cut


_K = _k_weight_power()
_BAND_OF = np.where(CENTRES < XOVER_LOW_HZ, 0, np.where(CENTRES < XOVER_HIGH_HZ, 1, 2))


def _spectra(x: np.ndarray, sr: int, t0: float, t1: float) -> tuple[np.ndarray, float]:
    """Channel-summed power per sub-band and STFT frame over [t0, t1] (s, clipped to the file), calibrated so a frame's
    sum is its mean square; and the time of frame 0."""
    i0 = max(0, int(t0 * sr))
    i1 = min(x.shape[-1], int(t1 * sr) + FFT)
    freqs = np.fft.rfftfreq(FFT, 1.0 / sr)
    bins = np.digitize(freqs, SUBBANDS) - 1
    win = np.hanning(FFT)
    norm = FFT * (win**2).sum() / 2.0
    n = max(0, (i1 - i0 - FFT) // HOP + 1)
    out = np.zeros((CENTRES.size, n))
    for ch in np.atleast_2d(x):
        for s in range(0, n, 256):
            idx = np.arange(s, min(n, s + 256))
            frames = np.stack([ch[i0 + j * HOP: i0 + j * HOP + FFT] for j in idx]) * win
            p = np.abs(np.fft.rfft(frames, axis=1)) ** 2 / norm
            for b in range(CENTRES.size):
                sel = bins == b
                if sel.any():
                    out[b, idx] += p[:, sel].sum(axis=1)
    return out, (i0 + FFT / 2) / sr


class _Side:
    """One deck: its sources' power on the grid, before any lane, and its lanes as arrays on the grid."""

    def __init__(self, deck: Deck, audio: DeckAudio, beats: np.ndarray) -> None:
        self.deck = deck
        self.beats = beats
        self.has_stems = bool(audio.stems)
        self.pinned = set(deck.pinned)
        sources = audio.stems if audio.stems else {"mix": audio.mix}
        times = np.asarray(deck_time(deck, beats), dtype=float)
        playing = (beats >= deck.play_from_beat) & (beats < deck.play_to_beat)
        heard = times[playing]
        trim = 10 ** (audio.trim_db / 10)
        self.sources: dict[str, np.ndarray] = {}
        for name, x in sources.items():
            out = np.zeros((CENTRES.size, beats.size))
            if heard.size:
                p, t_first = _spectra(x, audio.sr, max(0.0, heard.min() - 0.1), heard.max() + 0.1)
                if p.shape[1]:
                    fr = np.rint((times - t_first) * audio.sr / HOP).astype(int)
                    inside = playing & (fr >= 0) & (fr < p.shape[1]) & (times >= 0) & (times * audio.sr < x.shape[-1])
                    out[:, inside] = p[:, fr[inside]] * trim
            self.sources[name] = out
        self.reset()

    def reset(self) -> None:
        lanes = self.deck.lanes
        self.volume = np.asarray(lane_value(lanes.volume, self.beats), dtype=float)
        self.eq = {k: np.asarray(lane_value(getattr(lanes, k), self.beats), dtype=float) for k in EQ_BANDS}
        self.filter = np.asarray(lane_value(lanes.filter, self.beats), dtype=float)
        self.stem_gain: dict[str, np.ndarray] = {}
        if self.has_stems:
            stems = self.deck.stem_lanes
            for name in self.sources:
                lane = f"gain_{name}"
                if stems is not None and lane in STEM_LANES:
                    self.stem_gain[name] = np.asarray(lane_value(getattr(stems, lane), self.beats), dtype=float)
                else:
                    self.stem_gain[name] = np.ones(self.beats.size)

    def dry(self, stem_gains: dict[str, np.ndarray] | None = None) -> np.ndarray:
        """Sub-band power with the stems' gains, before EQ, filter and fader."""
        gains = self.stem_gain if stem_gains is None else stem_gains
        out = np.zeros((CENTRES.size, self.beats.size))
        for name, p in self.sources.items():
            g = gains.get(name)
            out += p if g is None else p * g**2
        return out

    def path_gain(self, skip_eq: str | None = None) -> np.ndarray:
        """Power gain per sub-band and grid point of the EQ, filter and fader."""
        g = np.ones((CENTRES.size, self.beats.size))
        for i, k in enumerate(EQ_BANDS):
            if k != skip_eq:
                g[_BAND_OF == i] *= _iso_power(self.eq[k])
        return g * filter_power(self.filter) * self.volume**2

    def power(self) -> np.ndarray:
        return self.dry() * self.path_gain()


def _iso_power(db: np.ndarray) -> np.ndarray:
    return (10 ** (db / 20) * np.clip((db + 30.0) / 6.0, 0.0, 1.0)) ** 2


def filter_hz(value: float) -> float:
    """The cutoff of a filter lane value: below 0 low-pass, above 0 high-pass, as the renderers do it."""
    m = abs(value)
    if value < 0:
        return FILTER_LP_HZ[1] * (FILTER_LP_HZ[0] / FILTER_LP_HZ[1]) ** m
    return FILTER_HP_HZ[0] * (FILTER_HP_HZ[1] / FILTER_HP_HZ[0]) ** m


def filter_value(hz: float, value_sign: float) -> float:
    """The lane value with this cutoff, on the side `value_sign` picks. Within -1..1."""
    if value_sign < 0:
        m = np.log(hz / FILTER_LP_HZ[1]) / np.log(FILTER_LP_HZ[0] / FILTER_LP_HZ[1])
        return -float(np.clip(m, 0.0, 1.0))
    return float(np.clip(np.log(hz / FILTER_HP_HZ[0]) / np.log(FILTER_HP_HZ[1] / FILTER_HP_HZ[0]), 0.0, 1.0))


def filter_power(value: np.ndarray) -> np.ndarray:
    """Power gain per sub-band and grid point of the 24 dB/oct DJ filter, dry below |value| = 0.03."""
    v = np.asarray(value, dtype=float)
    m = np.abs(v)
    lp = FILTER_LP_HZ[1] * (FILTER_LP_HZ[0] / FILTER_LP_HZ[1]) ** m
    hp = FILTER_HP_HZ[0] * (FILTER_HP_HZ[1] / FILTER_HP_HZ[0]) ** m
    f = CENTRES[:, None]
    gain = np.where(v < 0, 1.0 / (1.0 + (f / lp) ** 8), 1.0 / (1.0 + (hp / f) ** 8))
    wet = np.clip(m / 0.03, 0.0, 1.0)
    return 1.0 + (gain - 1.0) * wet


def spectral_centroid(power: np.ndarray) -> float | None:
    """Where a passage's energy sits, in Hz: the amplitude-weighted mean on a log frequency scale, so a kick doesn't
    decide it alone. `power` is (sub-bands, frames)."""
    weights = np.sqrt(power.mean(axis=1))
    total = weights.sum()
    return float(np.exp((weights * np.log(CENTRES)).sum() / total)) if total > 1e-12 else None


def onsets_per_beat(power: np.ndarray) -> float:
    """How busy a passage on the grid is: spectral-flux peaks per beat."""
    if power.shape[1] < GRID_PER_BEAT * 2:
        return 0.0
    log_p = np.log(power + 1e-10)
    flux = np.maximum(np.diff(log_p, axis=1), 0.0).sum(axis=0)
    if not flux.any():
        return 0.0
    threshold = flux.mean() + 0.5 * flux.std()
    peak = (flux[1:-1] > flux[:-2]) & (flux[1:-1] >= flux[2:]) & (flux[1:-1] > threshold)
    return float(peak.sum()) / (power.shape[1] / GRID_PER_BEAT)


def masking_index(lead: np.ndarray, other: np.ndarray) -> float:
    """The mix metrics' index: the share of two decks' energy common to both. 0: one owns the band, 1: an equal fight."""
    total = float((lead + other).sum())
    return 2.0 * float(np.minimum(lead, other).sum()) / total if total > 1e-14 else 0.0


# --- loudness --------------------------------------------------------------------------------------------------------


def _short_term(a: np.ndarray, b: np.ndarray, frames: int) -> np.ndarray:
    """The levelled sum's short-term LUFS per grid point."""
    p = (a * _K[:, None]).sum(axis=0) + (b * _K[:, None]).sum(axis=0)
    st = -0.691 + 10 * np.log10(ndimage.uniform_filter1d(p, max(1, frames), mode="nearest") + 1e-12)
    over = np.maximum(st - (DECK_LUFS + LEVEL_THRESHOLD_DB), 0.0)
    return st - over * (1 - 1 / LEVEL_RATIO) + MAKEUP_DB


def _line(st: np.ndarray, mask: np.ndarray, frames: int, ref_frames: int) -> np.ndarray | None:
    """The line through the overlap from A's level just before it to B's just after it."""
    idx = np.flatnonzero(mask)
    if not idx.size:
        return None
    o0, o1 = int(idx[0]), int(idx[-1]) + 1
    half = frames // 2
    before = st[max(0, o0 - half - ref_frames): max(0, o0 - half)]
    after = st[o1 + half: o1 + half + ref_frames]
    before, after = before[before > -70], after[after > -70]
    ref_a = float(np.median(before)) if before.size else None
    ref_b = float(np.median(after)) if after.size else None
    if ref_a is None and ref_b is None:
        return None
    ref_a, ref_b = (ref_a if ref_a is not None else ref_b), (ref_b if ref_b is not None else ref_a)
    return ref_a + (ref_b - ref_a) * np.clip((np.arange(st.size) - o0) / max(o1 - o0, 1), 0.0, 1.0)


# --- writing lanes ---------------------------------------------------------------------------------------------------


def _simplify(points: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    """Drops the points a straight line through their neighbours already gives; repeated beats (steps) stay."""
    out: list[tuple[float, float]] = []
    for i, p in enumerate(points):
        if out and i < len(points) - 1:
            prev, nxt = out[-1], points[i + 1]
            if nxt[0] > prev[0] and p[0] != prev[0] and p[0] != nxt[0]:
                t = (p[0] - prev[0]) / (nxt[0] - prev[0])
                if abs(prev[1] + (nxt[1] - prev[1]) * t - p[1]) <= tolerance:
                    continue
        out.append(p)
    return out


def _rewrite(lane: list[Keyframe], fn, b0: float, b1: float, lo: float, hi: float, tolerance: float) -> list[Keyframe]:
    """The lane with fn(beat) in place of its values over [b0, b1]; its keyframes outside stay. Steps stay steps."""
    knots = sorted({*(k.beat for k in lane if b0 < k.beat < b1), *np.arange(b0, b1, _LANE_STEP).tolist(), b1})
    points: list[tuple[float, float]] = []
    for b in knots:
        arrive, value = float(fn(b - _LEFT)), float(fn(b))
        if points and abs(arrive - value) > tolerance:
            points.append((b, arrive))
        points.append((b, value))
    points = [(b, float(np.clip(v, lo, hi))) for b, v in _simplify(points, tolerance)]
    before = [k for k in lane if k.beat < b0]
    after = [k for k in lane if k.beat > b1]
    tail = float(np.clip(fn(b1 + _LEFT), lo, hi))
    if not after and abs(tail - points[-1][1]) > tolerance:
        points.append((b1, tail))
    middle = [Keyframe(beat=b, value=v, curve="linear") for b, v in points]
    if not after:
        middle[-1] = Keyframe(beat=middle[-1].beat, value=middle[-1].value, curve="hold")
    return [*before, *middle, *after]


def _db_lane(lane: list[Keyframe], fn, b0: float, b1: float) -> list[Keyframe]:
    return _rewrite(lane, fn, b0, b1, *LANE_RANGES["eq_mid"], _SAME["db"])


# --- the steps -------------------------------------------------------------------------------------------------------


def _adapt_filter(side: _Side, deck: Deck, report: dict, key: str) -> Deck:
    if "filter" in side.pinned or all(abs(k.value) < 1e-3 for k in deck.lanes.filter):
        return deck
    active = np.abs(side.filter) >= 0.03
    centroid = spectral_centroid(side.dry()[:, active]) if active.any() else None
    if centroid is None:
        return deck
    ratio = float(np.clip(centroid / REF_CENTROID_HZ, *CENTROID_RATIO))
    keys = [
        k.model_copy(update={"value": filter_value(filter_hz(k.value) * ratio, k.value)}) if abs(k.value) >= 1e-3 else k
        for k in deck.lanes.filter
    ]
    report[f"{key}_centroid_hz"] = round(centroid)
    peak = max(keys, key=lambda k: abs(k.value))
    report[f"{key}_filter_peak_hz"] = round(filter_hz(peak.value))
    return deck.model_copy(update={"lanes": _lanes(deck, filter=keys)})


def _lanes(deck: Deck, **changes: list[Keyframe]) -> Lanes:
    return deck.lanes.model_copy(update=changes)


def _lerp(pair: tuple[float, float], t: float) -> float:
    return pair[0] + (pair[1] - pair[0]) * t


def _adapt_sends(side: _Side, deck: Deck, plan: MixPlan, end: float, report: dict) -> Deck:
    """The outgoing deck's effects, from its last phrase."""
    from_b, to_b = max(deck.play_from_beat, end - OUT_PHRASE_BEATS), min(deck.play_to_beat, end)
    sel = (side.beats >= from_b) & (side.beats < to_b)
    if not sel.any():
        return deck
    rate = onsets_per_beat(side.dry()[:, sel])
    t = float(np.clip((rate - DENSITY_ONSETS[0]) / (DENSITY_ONSETS[1] - DENSITY_ONSETS[0]), 0.0, 1.0))
    bpm = float(lane_value(plan.tempo, to_b - 1.0))
    report.update(out_onsets_per_beat=round(rate, 2), out_density=round(t, 2))
    sends = deck.sends
    update_sends: dict[str, float] = {}
    if "echo_beats" not in side.pinned:
        wanted_ms = _lerp(ECHO_MS, t)
        beats = min(ECHO_DIVISIONS, key=lambda d: abs(np.log(d * 60000.0 / bpm / wanted_ms)))
        update_sends["echo_beats"] = beats
        report["echo_beats"] = beats
    if "reverb_size" not in side.pinned:
        update_sends["reverb_size"] = round(_lerp(REVERB_SIZE, t), 3)
        report["reverb_size"] = update_sends["reverb_size"]
    scale, fb = _lerp(SEND_SCALE, t), _lerp(FEEDBACK_SCALE, t)
    changes: dict[str, list[Keyframe]] = {}
    for name, factor, top in (("echo_send", scale, 1.0), ("reverb_send", scale, 1.0), ("echo_feedback", fb, 0.95)):
        if name not in side.pinned:
            changes[name] = [k.model_copy(update={"value": min(top, k.value * factor)})
                             for k in getattr(deck.lanes, name)]  # fmt: skip
    report["send_scale"] = round(scale, 3)
    return deck.model_copy(update={"lanes": _lanes(deck, **changes), "sends": Sends(**{**sends.model_dump(),
                                                                                       **update_sends})})  # fmt: skip


class _Bars:
    """The bars of the blend, and curves with one value per bar: a bar's value is reached by its first beat, ramping
    over the bar before."""

    def __init__(self, beats: np.ndarray, mask: np.ndarray) -> None:
        bar = np.floor(beats / BEATS_PER_BAR).astype(int)
        keys = np.unique(bar[mask])
        self.starts = (keys * float(BEATS_PER_BAR)).tolist()
        self.sels = [(bar == k) & mask for k in keys]
        self.first = self.starts[0] if self.starts else 0.0
        self.end = self.starts[-1] + BEATS_PER_BAR if self.starts else 0.0

    def __len__(self) -> int:
        return len(self.starts)

    def held(self, values: np.ndarray, at: float | np.ndarray) -> np.ndarray:
        """The curve, holding its ends: for a cut, whose deck is silent beyond them."""
        return np.interp(at, self.starts, values)

    def faded(self, values: np.ndarray, at: float | np.ndarray) -> np.ndarray:
        """The curve, back at 0 a bar beyond either end: for a gain, which has to leave the tracks as they were."""
        xs = [self.first - BEATS_PER_BAR, *self.starts, self.end]
        return np.interp(at, xs, [0.0, *values, 0.0])


def _blend_mask(a: _Side, b: _Side) -> np.ndarray:
    """Grid points where both faders are up and both decks have something to play."""
    audible = 1e-3
    return (a.volume > audible) & (b.volume > audible) & (a.dry().sum(axis=0) > 0) & (b.dry().sum(axis=0) > 0)


def _close_low_gap(a: _Side, b: _Side, deck: Deck, mask: np.ndarray) -> tuple[Deck, float | None]:
    """A hand-off where A's lows are gone before B's come in leaves a hole the ear reads as a dip: B's lows come in as
    A's go. Returns the deck and the beat they moved to."""
    if "eq_low" in b.pinned or not mask.any():
        return deck, None
    beats = a.beats
    a_out = (a.eq["eq_low"] <= KILL_DB) | (a.volume < LOW_GONE_FADER)
    b_in = b.eq["eq_low"] > KILL_DB
    start = np.flatnonzero(mask)[0]
    gone = np.flatnonzero(a_out[start:] & ~b_in[start:])
    if not gone.size:
        return deck, None
    g = start + int(gone[0])
    later = np.flatnonzero(b_in[g:])
    if not later.size:
        return deck, None
    came_in = float(beats[g + int(later[0])])
    if not MIN_LOW_GAP_BEATS <= came_in - float(beats[g]) <= MAX_LOW_GAP_BEATS:
        return deck, None
    at = float(np.ceil(beats[g]))
    rest = [k for k in deck.lanes.eq_low if k.beat >= came_in]  # the first is where the planned ramp arrives
    landed = rest[0].value if rest else float(b.eq["eq_low"][g + int(later[0])])
    lane = [*(k for k in deck.lanes.eq_low if k.beat < at), Keyframe(beat=at, value=KILL_LANE_DB, curve="linear"),
            Keyframe(beat=at + 1.0, value=landed, curve="hold"), *rest[1:]]  # fmt: skip
    return deck.model_copy(update={"lanes": _lanes(deck, eq_low=lane)}), at


def _free_lows(a: _Side, b: _Side, bars: _Bars, hand: float) -> dict[str, np.ndarray]:
    """Bars where a killed low band has nothing to clash with: the lead's lows are LOW_FREE_DB under this deck's (A in a
    breakdown, say). Those kills open, at least MIN_FREE_BARS at a time. {"a": 0/1 per bar}"""
    out: dict[str, np.ndarray] = {}
    pa, pb = a.power(), b.power()
    low = _BAND_OF == 0
    opened = {id(a): a.dry() * a.volume**2 * filter_power(a.filter), id(b): b.dry() * b.volume**2 * filter_power(b.filter)}
    for side, name in ((a, "a"), (b, "b")):
        if "eq_low" in side.pinned:
            continue
        free = np.zeros(len(bars))
        for i, (start, sel) in enumerate(zip(bars.starts, bars.sels, strict=True)):
            follower, leader = _follower(a, b, start, hand)
            lead_power = pa if leader is a else pb
            mine = opened[id(side)][low][:, sel].sum()
            theirs = lead_power[low][:, sel].sum()
            killed = (side.eq["eq_low"][sel] <= KILL_DB).all()
            free[i] = follower is side and killed and mine > 0 and theirs < mine * 10 ** (-LOW_FREE_DB / 10)
        labels, n = ndimage.label(free)
        for run in range(1, n + 1):
            if (labels == run).sum() < MIN_FREE_BARS:
                free[labels == run] = 0.0
        if free.any():
            out[name] = free
            lift = bars.held(free, side.beats)
            inside = (side.beats >= bars.first) & (side.beats < bars.end)
            side.eq["eq_low"] = np.where(inside & (lift > 0.5), 0.0, side.eq["eq_low"])
    return out


def _hand_off(a: _Side, b: _Side, mask: np.ndarray) -> float:
    """The beat the lead passes from A to B: where the lows change hands, else the middle of the overlap."""
    beats = a.beats[mask]
    swap = np.flatnonzero((a.eq["eq_low"][mask] <= KILL_DB) & (b.eq["eq_low"][mask] > KILL_DB))
    return float(beats[swap[0]]) if swap.size else float((beats[0] + beats[-1]) / 2)


def _needed_cut(lead: np.ndarray, rest: np.ndarray, target: np.ndarray, limit: float) -> float:
    """The smallest cut (dB) of `target` that brings the masking index with `lead` down to TARGET_MASKING. `rest` is
    what the follower plays besides it."""
    for d in np.arange(0.0, limit + CUT_STEP_DB, CUT_STEP_DB):
        if masking_index(lead, rest + target * 10 ** (-d / 10)) <= TARGET_MASKING:
            return float(d)
    return limit


def _smooth_cuts(values: np.ndarray, following: np.ndarray) -> np.ndarray:
    """A bar's cut holds through its neighbours' (so the band doesn't pump bar to bar), to half a dB, and only while
    the deck is the follower: it is open again by the bar it leads."""
    return np.round(ndimage.maximum_filter1d(values, 3, mode="nearest") * 2) / 2 * following


def _follower(a: _Side, b: _Side, bar: float, hand: float) -> tuple[_Side, _Side]:
    """(the deck not leading in this bar, the one leading)."""
    return (b, a) if bar + BEATS_PER_BAR / 2 <= hand else (a, b)


def _cut_stems(a: _Side, b: _Side, bars: _Bars, hand: float) -> dict[str, float]:
    """Where a band is mostly one stem of the deck not leading, that stem takes the cut: nothing else of it suffers."""
    deepest: dict[str, float] = {}
    for side, name in ((a, "a"), (b, "b")):
        if not side.has_stems:
            continue
        path = side.path_gain()
        cuts = {s: np.zeros(len(bars)) for s in side.sources}
        for i, (start, sel) in enumerate(zip(bars.starts, bars.sels, strict=True)):
            follower, leader = _follower(a, b, start, hand)
            if follower is not side:
                continue
            lead_p = leader.power()[:, sel]
            for band in (1, 2):
                in_band = _BAND_OF == band
                lead = lead_p[in_band].sum(axis=0)
                stems = {s: (p[:, sel] * path[:, sel])[in_band].sum(axis=0) for s, p in side.sources.items()}
                total = sum(stems.values())
                if total.sum() <= 1e-14:
                    continue
                top = max(stems, key=lambda s: stems[s].sum())
                if stems[top].sum() / total.sum() >= STEM_SHARE:
                    d = _needed_cut(lead, total - stems[top], stems[top], MAX_STEM_CUT_DB)
                    cuts[top][i] = max(cuts[top][i], d)
        for stem, values in cuts.items():
            if f"gain_{stem}" in side.pinned or not values.any():
                continue
            smooth = _smooth_cuts(values, np.array([_follower(a, b, st, hand)[0] is side for st in bars.starts]))
            inside = (side.beats >= bars.first) & (side.beats < bars.end)
            side.stem_gain[stem] = side.stem_gain[stem] * np.where(inside, 10 ** (-bars.held(smooth, side.beats) / 20), 1.0)
            deepest[f"{name}.{stem}"] = float(smooth.max())
    return deepest


def _cut_bands(a: _Side, b: _Side, bars: _Bars, hand: float) -> dict[str, np.ndarray]:
    """Replaces the mids' and highs' planned cuts over the blend by what the audio needs. {"a.eq_mid": cut per bar}"""
    span = (a.beats >= bars.first) & (a.beats < bars.end)
    for s in (a, b):
        for k in CUT_BANDS:
            if k not in s.pinned:
                s.eq[k] = np.where(span & (s.eq[k] > PLANNED_DUCK_DB + 1e-6), 0.0, s.eq[k])
    pa, pb = a.power(), b.power()
    total = (pa + pb).sum(axis=0)
    cuts: dict[str, np.ndarray] = {}
    for side, name in ((a, "a"), (b, "b")):
        for i, band in enumerate(CUT_BANDS, start=1):
            if band in side.pinned:
                continue
            values = np.zeros(len(bars))
            in_band = _BAND_OF == i
            for j, (start, sel) in enumerate(zip(bars.starts, bars.sels, strict=True)):
                follower, leader = _follower(a, b, start, hand)
                if follower is not side:
                    continue
                other = (pa if follower is a else pb)[in_band][:, sel].sum(axis=0)
                lead = (pa if leader is a else pb)[in_band][:, sel].sum(axis=0)
                if (other + lead).sum() > 10 ** (QUIET_BAND_DB / 10) * total[sel].sum():
                    values[j] = _needed_cut(lead, np.zeros_like(other), other, MAX_CUT_DB)
            following = np.array([_follower(a, b, st, hand)[0] is side for st in bars.starts])
            cuts[f"{name}.{band}"] = smooth = _smooth_cuts(values, following)
            side.eq[band] = np.where(span, np.minimum(side.eq[band], -bars.held(smooth, side.beats)), side.eq[band])
    return cuts


def _ride(a: _Side, b: _Side, bars: _Bars, mask: np.ndarray, frames: int, ref_frames: int) -> tuple[np.ndarray, dict]:
    """Bar by bar, the gain (dB) that keeps the sum on the line between the tracks' levels."""
    pa, pb = a.power(), b.power()
    st = _short_term(pa, pb, frames)
    line = _line(st, mask, frames, ref_frames)
    if line is None:
        return np.zeros(len(bars)), {}
    before = (st - line)[mask]

    def rode(g: np.ndarray) -> np.ndarray:
        gain = 10 ** (bars.faded(g, a.beats) / 10)
        return _short_term(pa * gain, pb * gain, frames) - line

    gains = np.zeros(len(bars))
    for _ in range(RIDE_ITERATIONS):
        now = rode(gains)
        for i, sel in enumerate(bars.sels):
            dev = float(now[sel].mean())
            if abs(dev) > RIDE_DEAD_LU:
                gains[i] -= RIDE_GAIN * dev
        gains = np.clip(gains, *RIDE_BOUNDS)
    gains = np.round(gains * 4) / 4
    after = rode(gains)[mask]
    return gains, {
        "dip_lu_before": round(min(0.0, float(before.min())), 2), "bump_lu_before": round(max(0.0, float(before.max())), 2),
        "dip_lu": round(min(0.0, float(after.min())), 2), "bump_lu": round(max(0.0, float(after.max())), 2),
    }  # fmt: skip


def _ride_deck(deck: Deck, bars: _Bars, ride: np.ndarray) -> Deck:
    """A gain per bar on a deck's lanes: down on its fader, up on its EQ bands that aren't killed. Pinned lanes stay."""
    if not ride.size or not ride.any():
        return deck
    b0, b1 = bars.first - BEATS_PER_BAR, bars.end
    changes: dict[str, list[Keyframe]] = {}
    if ride.max() > 0:
        for band in EQ_BANDS:
            if band not in deck.pinned:
                lane = getattr(deck.lanes, band)

                def boosted(beat: float, lane: list[Keyframe] = lane) -> float:
                    base = float(lane_value(lane, beat))
                    return base + max(0.0, float(bars.faded(ride, beat))) if base > KILL_DB else base

                changes[band] = _db_lane(lane, boosted, b0, b1)
    if ride.min() < 0 and "volume" not in deck.pinned:
        def lowered(beat: float) -> float:
            return float(lane_value(deck.lanes.volume, beat)) * 10 ** (min(0.0, float(bars.faded(ride, beat))) / 20)

        changes["volume"] = _rewrite(deck.lanes.volume, lowered, b0, b1, 0.0, 1.0, _SAME["volume"])
    return deck.model_copy(update={"lanes": _lanes(deck, **changes)})


def _write_cuts(side: _Side, deck: Deck, name: str, bars: _Bars, cuts: dict[str, np.ndarray],
                free: dict[str, np.ndarray]) -> Deck:  # fmt: skip
    """The side's cuts back as keyframes over the blend."""
    b0, b1 = max(side.beats[0], bars.first - BEATS_PER_BAR), min(side.beats[-1], bars.end)
    changes: dict[str, list[Keyframe]] = {}
    for band in CUT_BANDS:
        cut = cuts.get(f"{name}.{band}")
        if cut is None or band in side.pinned:
            continue
        lane = getattr(deck.lanes, band)

        def fn(beat: float, lane: list[Keyframe] = lane, cut: np.ndarray = cut) -> float:
            base = float(lane_value(lane, beat))
            if bars.first <= beat < bars.end and base > PLANNED_DUCK_DB + 1e-6:
                return -float(bars.held(cut, beat))
            return base

        changes[band] = _db_lane(lane, fn, b0, b1)
    opened = free.get(name)
    if opened is not None:
        lane = deck.lanes.eq_low

        def low(beat: float) -> float:
            base = float(lane_value(lane, beat))
            if bars.first <= beat < bars.end and base <= KILL_DB:
                return base + (-base) * float(bars.held(opened, beat))
            return base

        changes["eq_low"] = _db_lane(lane, low, b0, b1)
    update: dict = {"lanes": _lanes(deck, **changes)} if changes else {}
    if side.has_stems:
        lanes: dict[str, list[Keyframe]] = {}
        for stem in ("drums", "bass", "vocals", "other"):
            lane_name = f"gain_{stem}"
            given = getattr(deck.stem_lanes, lane_name) if deck.stem_lanes else [Keyframe(beat=0.0, value=1.0)]
            gain = side.stem_gain.get(stem)
            if lane_name in side.pinned or gain is None or (gain == 1.0).all():
                lanes[lane_name] = given
                continue

            def sg(beat: float, gain: np.ndarray = gain) -> float:
                return float(np.interp(beat, side.beats, gain))

            lanes[lane_name] = _rewrite(given, sg, b0, b1, 0.0, 1.0, _SAME["gain"])
        update["stem_lanes"] = StemLanes(**lanes)
    return deck.model_copy(update=update) if update else deck


def ride_measured(plan: MixPlan, beats: np.ndarray, deviation: np.ndarray) -> MixPlan | None:
    """The plan corrected for loudness measured on a render: `deviation` (LU against the line between the tracks) at
    master `beats`. What a prediction can't see, the leveller and effect returns, shows up here. None: nothing to do."""
    if not beats.size:
        return None
    bars = _Bars(beats, np.ones(beats.size, bool))
    worst = [float(deviation[sel][np.argmax(np.abs(deviation[sel]))]) for sel in bars.sels]  # the metric reads peaks
    ride = np.array([-MEASURED_GAIN * d if abs(d) > MEASURED_TRIGGER_LU else 0.0 for d in worst])
    ride = np.round(np.clip(ride, *RIDE_BOUNDS) * 4) / 4
    if not ride.any():
        return None
    update = {"a": _ride_deck(plan.a, bars, ride), "b": _ride_deck(plan.b, bars, ride)}
    return MixPlan.model_validate(plan.model_copy(update=update).model_dump())


# --- the whole plan --------------------------------------------------------------------------------------------------


def _stem_pairs(a: _Side, b: _Side, mask: np.ndarray) -> dict[str, dict[str, float]]:
    """The masking index of every pair of the decks' sources over the overlap, per band. "mix" for a deck without stems:
    what an editor shows as "A's vocals against B's other"."""
    out: dict[str, dict[str, float]] = {}
    ga, gb = a.path_gain()[:, mask], b.path_gain()[:, mask]
    for sa, pa in a.sources.items():
        for sb, pb in b.sources.items():
            row = {}
            xa, xb = pa[:, mask] * ga, pb[:, mask] * gb
            for i, band in enumerate(("low", "mid", "high")):
                sel = _BAND_OF == i
                row[band] = round(masking_index(xa[sel].sum(axis=0), xb[sel].sum(axis=0)), 3)
            out[f"{sa}/{sb}"] = row
    return out


def _band_masking(a: _Side, b: _Side, mask: np.ndarray) -> dict[str, float]:
    pa, pb = a.power()[:, mask], b.power()[:, mask]
    return {band: round(masking_index(pa[_BAND_OF == i].sum(axis=0), pb[_BAND_OF == i].sum(axis=0)), 3)
            for i, band in enumerate(("low", "mid", "high"))}  # fmt: skip


def adapt(plan: MixPlan, audio_a: DeckAudio, audio_b: DeckAudio) -> Adapted:
    """The plan with its automation fitted to these two tracks, and a report of what was measured and decided (for the
    editor and the eval). See the module's docstring."""
    if plan.edited:
        return Adapted(plan, {"skipped": "edited"})
    w0, w1 = plan.window.start_beat, plan.window.end_beat
    beats = np.arange(w0, w1, 1.0 / GRID_PER_BEAT)
    a_deck, b_deck = plan.a, plan.b
    a, b = _Side(a_deck, audio_a, beats), _Side(b_deck, audio_b, beats)
    report: dict = {}

    a_deck = _adapt_filter(a, a_deck, report, "a")
    b_deck = _adapt_filter(b, b_deck, report, "b")
    a.deck, b.deck = a_deck, b_deck
    a.reset()
    b.reset()

    mask = _blend_mask(a, b)
    end = float(beats[mask][-1]) + 1.0 / GRID_PER_BEAT if mask.any() else plan.a.play_to_beat
    a_deck = _adapt_sends(a, a_deck, plan, end, report)
    a.deck = a_deck

    if mask.sum() >= GRID_PER_BEAT * BEATS_PER_BAR:
        b_deck, moved = _close_low_gap(a, b, b_deck, mask)
        if moved is not None:
            report["lows_moved_to_beat"] = moved
            b.deck = b_deck
            b.eq["eq_low"] = np.asarray(lane_value(b_deck.lanes.eq_low, beats), dtype=float)
        spb = 60.0 / float(np.mean(lane_value(plan.tempo, beats)))
        frames = max(1, round(SHORT_TERM_S / (spb / GRID_PER_BEAT)))
        ref_frames = max(1, round(REF_S / (spb / GRID_PER_BEAT)))
        bars = _Bars(beats, mask)
        report["masking_before"] = _band_masking(a, b, mask)
        hand = _hand_off(a, b, mask)
        report["hand_off_beat"] = hand
        stem_cuts = _cut_stems(a, b, bars, hand)
        if stem_cuts:
            report["stem_cuts_db"] = stem_cuts
        free = _free_lows(a, b, bars, hand)
        if free:
            report["lows_opened_bars"] = {k: int(v.sum()) for k, v in free.items()}
        cuts = _cut_bands(a, b, bars, hand)
        report["cuts_db"] = {k: float(v.max()) for k, v in cuts.items()}
        report["masking"] = _band_masking(a, b, mask)
        report["stem_masking"] = _stem_pairs(a, b, mask)
        ride, rode = _ride(a, b, bars, mask, frames, ref_frames)
        report.update(rode)
        report["ride_db"] = [round(float(ride.min()), 2), round(float(ride.max()), 2)]
        a_deck = _ride_deck(_write_cuts(a, a_deck, "a", bars, cuts, free), bars, ride)
        b_deck = _ride_deck(_write_cuts(b, b_deck, "b", bars, cuts, free), bars, ride)
    notes = [*plan.notes, *_notes(report)]
    return Adapted(MixPlan.model_validate(plan.model_copy(update={"a": a_deck, "b": b_deck, "notes": notes[-64:]}).model_dump()),
                   report)  # fmt: skip


def _notes(report: dict) -> list[str]:
    out = []
    cuts = {k: v for k, v in report.get("cuts_db", {}).items() if v > 0}
    if cuts:
        names = {"eq_mid": "mids", "eq_high": "highs"}
        said = ", ".join(f"{k[0].upper()}'s {names[k[2:]]} up to {v:.0f} dB" for k, v in sorted(cuts.items()))
        out.append(f"Cut only where the tracks collide: {said}.")
    elif "cuts_db" in report:
        out.append("The tracks' mids and highs don't collide: no cuts.")
    if report.get("stem_cuts_db"):
        said = ", ".join(f"{k[0].upper()}'s {k[2:]} {v:.0f} dB" for k, v in sorted(report["stem_cuts_db"].items()))
        out.append(f"Stems turned down where one stem makes the clash: {said}.")
    if "ride_db" in report and report["ride_db"] != [0.0, 0.0]:
        lo, hi = report["ride_db"]
        out.append(f"Gain riding {lo:+.1f} to {hi:+.1f} dB keeps the loudness on the line between the tracks.")
    if "echo_beats" in report:
        out.append(f"Echo every {report['echo_beats']:g} beats, sends x{report['send_scale']:.2f}: the last phrase has "
                   f"{report['out_onsets_per_beat']:.1f} onsets a beat.")  # fmt: skip
    return out


def with_pins(generated: MixPlan, previous: MixPlan) -> MixPlan:
    """A freshly generated plan, ready for `adapt`, with what a person pinned on the previous one. Each deck's pinned
    lanes and send settings are copied over; the rest is the new plan's."""
    update = {}
    for name in ("a", "b"):
        new, old = getattr(generated, name), getattr(previous, name)
        lanes = {k: getattr(old.lanes, k) for k in old.pinned if k in Lanes.model_fields}
        stems = {k: getattr(old.stem_lanes, k) for k in old.pinned if k in STEM_LANES and old.stem_lanes is not None}
        sends = {k: getattr(old.sends, k) for k in old.pinned if k in Sends.model_fields}
        changes: dict = {"pinned": old.pinned, "lanes": new.lanes.model_copy(update=lanes),
                         "sends": new.sends.model_copy(update=sends)}  # fmt: skip
        if stems:
            base = new.stem_lanes or StemLanes(**{k: [Keyframe(beat=0.0, value=1.0)] for k in STEM_LANES})
            changes["stem_lanes"] = base.model_copy(update=stems)
        update[name] = new.model_copy(update=changes)
    return generated.model_copy(update=update)
