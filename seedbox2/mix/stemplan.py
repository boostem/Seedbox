"""Stem-aware transitions (#53): the hand-offs only stems can do cleanly, planned ahead with both tracks known.

`plan(generated, a, b)` takes a generated plan (Seedbox 1's generator, converted) and what is known of the two tracks
(`Track`: stems or not, key, vocal phrases, acapella and instrumental windows, phrase starts, stem activity) and
rewrites the blend on the stem lanes:

- **Bass**: one hand-off beat on a phrase boundary inside the blend (`bass_beat`: B's phrase start nearest to where the
  generator brought B's lows in). A's bass stem goes to 0 over the quarter beat before it, B's comes up over the
  quarter beat after: never two basslines, and silence rather than an EQ kill's -30 dB. The generator's low EQ stays
  (it keeps kicks out too), except that B's lows open on the hand-off at the latest; a deck without stems gets the bass
  lane folded into its lows (`plan.fold_stem_lanes`), which is an EQ swap on the same beat. With stems, A's lows also
  carry to the hand-off, so there's no hole. Between two decks without stems the generator's EQ swap is the fallback
  and stays as planned.
- **Vocals**: where both decks would sing at once, one vocal stem is muted for the overlap (B's before the bass
  hand-off, A's from it on; the other one when that deck has no stems), with a beat's fade each side. Seedbox 1's
  -18 dB duck of the whole mid band is taken out of decks with stems. When A's phrase ends before B's starts nothing
  is touched; between two decks without stems the duck stays, as the fallback.
- **Drums**: B's drums come in first under a high-pass (`KICK_HP_HZ`) up to the hand-off; from the hand-off A's drums
  play out last under the same high-pass. Hats and percussion layer; the kicks never sound together. Where a deck's
  lows are killed anyway (the generator's EQ, or a bass lane folded into it) the kick is out already and the filter is
  left as planned.
- **Mashup moment**: when the keys are compatible (same, ±1 or relative, after `key_shift`) and the windows line up,
  8 or 16 bars of B's acapella over A's instrumental before the hand-off, or A's acapella over B's instrumental after
  it. B may be moved by whole 8-bar phrases (`MASHUP_SHIFTS`) to line them up.

The rules are also checks (`check`) on the plan as it plays, with the stems folded for decks that have none: the bass
overlap energy (dB of the shared low end against the larger), the seconds two vocals are heard together and the
seconds two unfiltered kicks are. `LIMITS` holds them, and the planner keeps to them: the forces it applies are in the
report, and `enforce` puts them back on a plan that `automation.adapt` or the measured gain riding changed since.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..analysis.grid import Grid
from ..sets.cost import UNKNOWN_KEY, key_index, shifted
from .automation import filter_hz, filter_value
from .plan import (
    LANE_DEFAULTS, STEM_LANES, Deck, Keyframe, MixPlan, StemLanes, deck_grid, deck_time, fold_stem_lanes, lane_value,
)  # fmt: skip

KICK_HP_HZ = 250.0  # a high-pass this high takes a kick's body out and leaves the hats
KICK_FILTER = round(filter_value(KICK_HP_HZ, 1.0), 3)
BASS_RAMP = 0.25  # beats: A's bass out before the hand-off beat, B's in after it
VOCAL_RAMP = 1.0
AUDIBLE = 0.05  # a fader above this is heard: the blend is where both are
VOCAL_HEARD = 0.25  # -12 dB: a vocal this loud counts toward an overlap
KICK_HEARD = 0.25
KICK_HZ = 60.0
KICK_KILLED_DB = -20.0  # a low EQ this far down already keeps the kick out
BASS_HZ = 60.0
STEP = 0.25  # beats between the checks' samples
MASHUP_BARS = (16, 8)  # the longest that fits
MASHUP_SUNG = 0.75  # the acapella deck sings this share of the window
MASHUP_QUIET = 0.1  # ... and the instrumental deck at most this share
FADE_SLACK = 1.0  # beats: a window may start while a fader is still coming up, or end as it goes
MASHUP_SHIFTS = (0, 32, -32, 64, -64, 96, -96, 128, -128)  # beats B may move to line them up: whole 8-bar phrases
PHRASE_SNAP = 0.15  # beats: a phrase start this close to a master beat is on it
VOCAL_DUCK_DB = -18.0  # Seedbox 1's mid duck (musicreco.mixplan._VOCAL_CUT)
LIMITS = {"bass_overlap_db": -20.0, "vocal_overlap_s": 0.5, "kick_double_s": 0.25}
_EPS = 1e-6
_SAME = 1e-4
_SILENT_DB = -120.0
_MAX_NOTES = 64

Spans = list[tuple[float, float]]  # file seconds
Activity = Callable[[str, np.ndarray], np.ndarray]  # (stem, file seconds) -> 0..1


@dataclass
class Track:
    """What the planner knows of one track, in its file's seconds. `vocals` None: not known (no rule, no check)."""

    has_stems: bool = False
    camelot: str | None = None
    vocals: Spans | None = None
    acapella: Spans = field(default_factory=list)
    instrumental: Spans = field(default_factory=list)
    phrase_starts: list[float] = field(default_factory=list)
    activity: Activity | None = None  # None: every stem plays throughout


def _spans(items: Any) -> Spans:
    return [(float(s["start_s"]), float(s["end_s"])) for s in items or [] if s.get("end_s", 0) > s.get("start_s", 0)]


def track_from_analysis(an: dict, *, has_stems: bool, camelot: str | None = None,
                        sections: dict | None = None) -> Track:
    """From a Seedbox 1-shaped analysis (`vocals.plan_input` puts the vocal stem's phrases in it) and the track's
    `sections` analysis. Without the stem's phrases, bars with `vocal_per_bar` >= 0.5 are the vocals."""
    if an.get("vocal_phrases") is not None:
        voc: Spans | None = _spans(an["vocal_phrases"])
    elif an.get("vocal_per_bar") is not None and an.get("bar_s"):
        fd, bar = float(an.get("first_downbeat_s") or 0.0), float(an["bar_s"])
        on = np.asarray(an["vocal_per_bar"], float) >= 0.5
        voc = [(fd + a * bar, fd + b * bar) for a, b in _runs(on)]
    else:
        voc = None
    activity = None
    if sections and sections.get("activity") and an.get("grid"):
        activity = _bar_activity(Grid.from_json(an["grid"]), float(sections.get("first_bar_beat", 0.0)),
                                 sections["activity"])  # fmt: skip
    return Track(has_stems=has_stems, camelot=camelot, vocals=voc, acapella=_spans(an.get("vocal_acapella")),
                 instrumental=_spans(an.get("vocal_instrumental")),
                 phrase_starts=[float(p["start_s"]) for p in (sections or {}).get("phrases") or []],
                 activity=activity)  # fmt: skip


def _bar_activity(grid: Grid, first_bar_beat: float, per_bar: dict[str, list[float]]) -> Activity:
    arrays = {k: np.asarray(v, float) for k, v in per_bar.items()}

    def at(stem: str, t: np.ndarray) -> np.ndarray:
        v = arrays.get(stem)
        if v is None or not v.size:
            return np.ones_like(t, dtype=float)
        bar = np.floor((np.asarray(grid.beat_at(t), float) - first_bar_beat) / 4.0).astype(int)
        return np.where((bar >= 0) & (bar < v.size), v[np.clip(bar, 0, v.size - 1)], 0.0)

    return at


ENVELOPE_S = 0.25


def track_from_audio(stems: dict[str, np.ndarray] | None, sr: int, grid: Grid, *, camelot: str | None = None,
                     phrase_starts: list[float] | None = None) -> Track:  # fmt: skip
    """From decoded stems (channels, n) at `sr`: the vocal stem's phrases and windows (`analysis.vocals`, on `grid`) and
    each stem's level envelope against its own loud parts. Without stems only the key and phrases are known."""
    if not stems:
        return Track(camelot=camelot, phrase_starts=phrase_starts or [])
    from scipy.signal import resample_poly  # noqa: PLC0415

    from ..analysis import vocals  # noqa: PLC0415
    from ..analysis.waveform import SAMPLE_RATE  # noqa: PLC0415

    def mono(x: np.ndarray) -> np.ndarray:
        return np.asarray(x, np.float64).reshape(-1, np.shape(x)[-1]).mean(axis=0)

    down = (lambda x: x) if sr == SAMPLE_RATE else (lambda x: resample_poly(x, SAMPLE_RATE, sr, axis=-1))
    vocal = down(np.asarray(stems["vocals"], np.float64).reshape(-1, np.shape(stems["vocals"])[-1])).astype(np.float32)
    rest = down(sum(mono(x) for k, x in stems.items() if k != "vocals")).astype(np.float32)
    data = vocals.analyse_arrays(vocal, rest, grid)
    hop = max(1, int(ENVELOPE_S * sr))
    env = {}
    for name, x in stems.items():
        m = mono(x)
        n = m.size // hop
        rms = np.sqrt(np.mean(m[: n * hop].reshape(n, hop) ** 2, axis=1)) if n else np.zeros(1)
        ref = float(np.percentile(rms, 95)) if rms.size else 0.0
        env[name] = np.clip(rms / ref, 0.0, 1.0) if ref > 1e-6 else np.zeros_like(rms)

    def at(stem: str, t: np.ndarray) -> np.ndarray:
        v = env.get(stem)
        if v is None:
            return np.ones_like(t, dtype=float)
        i = np.floor(np.asarray(t, float) / ENVELOPE_S).astype(int)
        return np.where((i >= 0) & (i < v.size), v[np.clip(i, 0, v.size - 1)], 0.0)

    return Track(has_stems=True, camelot=camelot, vocals=_spans(data["phrases"]), acapella=_spans(data["acapella"]),
                 instrumental=_spans(data["instrumental"]), phrase_starts=phrase_starts or [], activity=at)  # fmt: skip


# --- lanes -----------------------------------------------------------------------------------------------------------


def _runs(on: np.ndarray) -> list[tuple[int, int]]:
    edges = np.diff(np.concatenate(([0], on.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(edges == 1).tolist(), np.flatnonzero(edges == -1).tolist()))


def _kfs(lane: list[Keyframe]) -> list[dict]:
    return [k.model_dump() for k in lane]


def _rewrite(lane: list[Keyframe], s: float, e: float, fn: Callable[[float, float], float]) -> list[Keyframe]:
    """The lane with [s, e] replaced by fn(beat, value), sampled every STEP beats (linear between); outside unchanged,
    with steps where the new values meet the old at s and e."""
    before = [k for k in lane if k.beat < s - _EPS]
    after = [k for k in lane if k.beat > e + _EPS]
    out = [*before, Keyframe(beat=s, value=float(lane_value(lane, s - _EPS)), curve="linear")]  # arriving as it was
    n = max(1, math.ceil((e - s) / STEP - _EPS))
    inner = sorted({s + (e - s) * i / n for i in range(n + 1)} | {k.beat for k in lane if s < k.beat < e})
    tail = float(lane_value(lane, e))
    for x in inner:
        v = float(lane_value(lane, x))
        if x >= e:  # the value the lane arrives with, unless that is the same as the one it leaves with
            arrive = float(lane_value(lane, e - _EPS))
            v = tail if abs(arrive - tail) < _SAME else arrive
        out.append(Keyframe(beat=x, value=fn(x, v), curve="linear"))
    governing = next((k.curve for k in reversed(lane) if k.beat <= e + _EPS), "hold")
    if after or abs(tail - out[-1].value) > _EPS:
        out.append(Keyframe(beat=e, value=tail, curve=governing))
    out += after
    return _simplify(out)


def _simplify(lane: list[Keyframe]) -> list[Keyframe]:
    """Drops keyframes in the middle of a straight run of equal values."""
    out: list[Keyframe] = []
    for k in lane:
        if out and abs(out[-1].beat - k.beat) < _EPS and abs(out[-1].value - k.value) < _EPS:
            out[-1] = k
            continue
        if (len(out) >= 2 and abs(out[-1].value - k.value) < _EPS and abs(out[-2].value - k.value) < _EPS
                and out[-2].beat < out[-1].beat < k.beat):  # fmt: skip
            out[-1] = Keyframe(beat=k.beat, value=k.value, curve=k.curve)
            continue
        out.append(k)
    return out


def _mask(s: float, e: float, ramp_in: float, ramp_out: float) -> Callable[[float], float]:
    """1 outside [s - ramp_in, e + ramp_out], 0 inside [s, e], linear between."""

    def m(x: float) -> float:
        if s <= x <= e:
            return 0.0
        if x < s:
            return 1.0 if ramp_in <= 0 else float(np.clip((s - x) / ramp_in, 0.0, 1.0))
        return 1.0 if ramp_out <= 0 else float(np.clip((x - e) / ramp_out, 0.0, 1.0))

    return m


@dataclass(frozen=True)
class Force:
    """One rule as applied: `mute` holds a stem lane at 0 over [start, end] and under linear ramps outside it, `floor`
    holds a lane (the filter, the lows) at least at `value` there. Applying one twice changes nothing."""

    deck: str
    lane: str
    kind: str
    start: float
    end: float
    value: float = 0.0
    ramp_in: float = 0.0
    ramp_out: float = 0.0

    def apply(self, deck: Deck) -> Deck:
        if self.kind == "floor":
            lane = _rewrite(getattr(deck.lanes, self.lane), self.start, self.end, lambda _x, v: max(v, self.value))
            return deck.model_copy(update={"lanes": deck.lanes.model_copy(update={self.lane: lane})})
        stems = deck.stem_lanes or _flat_stems()
        m = _mask(self.start, self.end, self.ramp_in, self.ramp_out)
        lane = _rewrite(getattr(stems, self.lane), self.start - self.ramp_in, self.end + self.ramp_out,
                        lambda x, v: min(v, m(x)))  # fmt: skip
        return deck.model_copy(update={"stem_lanes": stems.model_copy(update={self.lane: lane})})

    def to_json(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def _flat_stems() -> StemLanes:
    return StemLanes.model_validate({name: [{"beat": 0.0, "value": LANE_DEFAULTS[name], "curve": "hold"}]
                                     for name in STEM_LANES})  # fmt: skip


def apply_forces(plan: MixPlan, forces: list[Force]) -> MixPlan:
    decks = {"a": plan.a, "b": plan.b}
    for f in forces:
        decks[f.deck] = f.apply(decks[f.deck])
    return plan.model_copy(update=decks)


def enforce(plan: MixPlan, report: dict) -> MixPlan:
    """The plan with the forces `plan` applied (its report's `forces`) put back: after `automation.adapt` or the
    measured gain riding moved lanes, the rules hold again."""
    forces = [Force(**f) for f in report.get("forces") or []]
    return apply_forces(plan, forces) if forces and not plan.edited else plan


# --- what the plan does, sampled -------------------------------------------------------------------------------------


@dataclass
class _Side:
    deck: Deck
    track: Track
    beats: np.ndarray

    def __post_init__(self) -> None:
        d = self.deck
        self.t = np.asarray(deck_time(d, self.beats), float)
        self.playing = (self.beats >= d.play_from_beat) & (self.beats < d.play_to_beat)
        self.volume = np.where(self.playing, np.asarray(lane_value(d.lanes.volume, self.beats), float), 0.0)

    def sung(self) -> np.ndarray:
        if self.track.vocals is None:
            return np.zeros(self.beats.size, bool)
        on = np.zeros(self.beats.size, bool)
        for s, e in self.track.vocals:
            on |= (self.t >= s) & (self.t < e)
        return on

    def windows(self, spans: Spans) -> np.ndarray:
        on = np.zeros(self.beats.size, bool)
        for s, e in spans:
            on |= (self.t >= s) & (self.t < e)
        return on

    def activity(self, stem: str) -> np.ndarray:
        if self.track.activity is None:
            return np.ones(self.beats.size)
        return np.asarray(self.track.activity(stem, self.t), float)

    def lane(self, name: str) -> np.ndarray:
        if name.startswith("gain_"):
            if self.deck.stem_lanes is None:
                return np.ones(self.beats.size)
            return np.asarray(lane_value(getattr(self.deck.stem_lanes, name), self.beats), float)
        return np.asarray(lane_value(getattr(self.deck.lanes, name), self.beats), float)

    def hp_gain(self, hz: float) -> np.ndarray:
        """What the deck's filter leaves of `hz` (24 dB/oct high-pass; a low-pass passes it)."""
        f = self.lane("filter")
        fc = np.array([filter_hz(v) for v in f])
        return np.where((f > 0.03) & (fc > hz), (hz / np.maximum(fc, 1e-9)) ** 4, 1.0)


def _sample(plan: MixPlan) -> np.ndarray:
    return np.arange(plan.window.start_beat, plan.window.end_beat, STEP)


def blend(plan: MixPlan) -> tuple[float, float] | None:
    """The master beats where both faders are up, or None when that's under a bar (a cut)."""
    beats = _sample(plan)
    a_vol = np.asarray(lane_value(plan.a.lanes.volume, beats), float)
    b_vol = np.asarray(lane_value(plan.b.lanes.volume, beats), float)
    both = ((a_vol > AUDIBLE) & (beats >= plan.a.play_from_beat) & (beats < plan.a.play_to_beat)
            & (b_vol > AUDIBLE) & (beats >= plan.b.play_from_beat) & (beats < plan.b.play_to_beat))  # fmt: skip
    if both.sum() * STEP < 4.0:
        return None
    on = np.flatnonzero(both)
    return float(beats[on[0]]), float(beats[on[-1]] + STEP)


# --- the rules -------------------------------------------------------------------------------------------------------


def _bass_in(plan: MixPlan, span: tuple[float, float]) -> float:
    """Where the generated plan brings B's lows in: its low EQ up, inside the blend; the blend's end without one."""
    beats = np.arange(span[0], span[1], STEP)
    up = np.asarray(lane_value(plan.b.lanes.eq_low, beats), float) > -6.0
    return float(beats[np.argmax(up)]) if up.any() else span[1]


def _lows_out(deck: Deck, span: tuple[float, float]) -> float:
    """Where the deck's low EQ first goes down inside the span; its end if it doesn't."""
    beats = np.arange(span[0], span[1], STEP)
    down = np.asarray(lane_value(deck.lanes.eq_low, beats), float) <= -6.0
    return float(beats[np.argmax(down)]) if down.any() else span[1]


def bass_beat(plan: MixPlan, b: Track, span: tuple[float, float]) -> float:
    """The bass hand-off: a phrase boundary of B inside the blend (a bar after it starts at the earliest, its end at the
    latest), the one nearest to where the generator brought B's lows in. Without B's phrases, the 4-bar grid of the
    master timeline from beat 0, where B came in on a phrase."""
    target = _bass_in(plan, span)
    lo, hi = span[0] + 4.0, span[1]
    grid = deck_grid(plan.b)
    starts = [float(grid.beat_at(t)) for t in b.phrase_starts]
    cands = [round(x) for x in starts if abs(x - round(x)) <= PHRASE_SNAP and lo <= round(x) <= hi]
    if not cands:
        cands = [16.0 * k for k in range(math.ceil(lo / 16.0), math.floor(hi / 16.0) + 1)]
    if not cands:
        return float(round(min(max(target, lo), hi)))
    return float(min(cands, key=lambda x: (abs(x - target), x)))


def keys_compatible(a: Track, b: Track, plan: MixPlan) -> bool:
    """Same key, a step round the wheel or the relative key, as heard (after each deck's key_shift)."""
    ka, kb = key_index(a.camelot), key_index(b.camelot)
    if UNKNOWN_KEY in (ka, kb):
        return False
    ka, kb = shifted(ka, plan.a.key_shift), shifted(kb, plan.b.key_shift)
    d = (ka % 12 - kb % 12) % 12
    steps = min(d, 12 - d)
    return steps == 0 or (steps == 1 and (ka >= 12) == (kb >= 12))


def shift_b(plan: MixPlan, beats: float) -> MixPlan | None:
    """B moved `beats` later in its own file (master beat m plays what m + beats played), or None if that leaves the
    plan invalid (B before its file's start where it's heard, or ending inside the window)."""
    if not beats:
        return plan
    b = plan.b
    grid = [s.model_copy(update={"start_beat": s.start_beat - beats}) for s in b.grid]
    to = b.play_to_beat - beats
    if to <= b.play_from_beat or max(plan.a.play_to_beat, to) < plan.window.end_beat:
        return None
    moved = b.model_copy(update={"grid": grid, "play_to_beat": to})
    if float(deck_time(moved, max(b.play_from_beat, plan.window.start_beat))) < 0.0:
        return None
    return plan.model_copy(update={"b": moved})


def _share(mask: np.ndarray, sel: np.ndarray) -> float:
    return float(mask[sel].mean()) if sel.any() else 0.0


def find_mashup(plan: MixPlan, a: Track, b: Track) -> dict | None:
    """The first lined-up mashup moment, B moved as little as it takes: {"shift", "direction", "start", "end",
    "bass_beat"}. Forward is B's acapella over A's instrumental for the 8-16 bars up to the bass hand-off, reverse A's
    acapella over B's instrumental for those after it."""
    if not keys_compatible(a, b, plan) or a.vocals is None or b.vocals is None:
        return None
    for shift in MASHUP_SHIFTS:
        moved = shift_b(plan, shift)
        span = blend(moved) if moved is not None else None
        if span is None:
            continue
        hand = bass_beat(moved, b, span)
        beats = _sample(moved)
        sa, sb = _Side(moved.a, a, beats), _Side(moved.b, b, beats)
        sung_a, sung_b = sa.sung() | sa.windows(a.acapella), sb.sung() | sb.windows(b.acapella)
        for bars in MASHUP_BARS:
            for direction, w0, w1 in (("forward", hand - 4.0 * bars, hand), ("reverse", hand, hand + 4.0 * bars)):
                sel = (beats >= w0) & (beats < w1)
                if w0 < span[0] - FADE_SLACK or w1 > span[1] + FADE_SLACK:
                    continue
                voice, music = (b, a) if direction == "forward" else (a, b)
                sung_v, sung_m = (sung_b, sung_a) if direction == "forward" else (sung_a, sung_b)
                if not voice.has_stems or _share(sung_v, sel) < MASHUP_SUNG or _share(sung_m, sel) > MASHUP_QUIET:
                    continue
                return {"shift": shift, "direction": direction, "start": w0, "end": w1, "bars": bars,
                        "bass_beat": hand}  # fmt: skip
    return None


def _no_duck(deck: Deck) -> tuple[Deck, int]:
    """Seedbox 1's vocal duck taken out of the mids: its -18 dB keyframes dropped, the lane joining up around them."""
    keep = [k for k in deck.lanes.eq_mid if abs(k.value - VOCAL_DUCK_DB) > 1e-6]
    dropped = len(deck.lanes.eq_mid) - len(keep)
    if not dropped:
        return deck, 0
    keep = keep or [Keyframe(beat=deck.lanes.eq_mid[0].beat, value=0.0, curve="hold")]
    return deck.model_copy(update={"lanes": deck.lanes.model_copy(update={"eq_mid": keep})}), dropped


def _vocal_mutes(plan: MixPlan, a: Track, b: Track, span: tuple[float, float],
                 hand: float) -> tuple[list[Force], list[str]]:
    """A mute for every stretch both decks would sing in the blend: the deck not leading (B before the hand-off, A from
    it), or the other one when that deck has no stems. None between two decks without stems: Seedbox 1's duck stays."""
    if a.vocals is None or b.vocals is None:
        return [], ["Vocals: not known for both tracks, nothing changed."]
    beats = np.arange(span[0], span[1], STEP)
    sa, sb = _Side(plan.a, a, beats), _Side(plan.b, b, beats)
    both = sa.sung() & sb.sung()
    forces, notes = [], []
    for i, j in _runs(both):
        s, e = float(beats[i]), float(beats[j - 1] + STEP)
        for p0, p1, lead in ((s, min(e, hand), "a"), (max(s, hand), e, "b")):
            if p1 - p0 < STEP / 2:
                continue
            quiet = "b" if lead == "a" else "a"
            if not (b if quiet == "b" else a).has_stems:
                quiet = lead
                if not (b if quiet == "b" else a).has_stems:
                    notes.append(f"Vocals clash over beats {p0:g}-{p1:g}; neither deck has stems: the mids duck.")
                    continue
            forces.append(Force(quiet, "gain_vocals", "mute", round(p0, 3), round(p1, 3),
                                ramp_in=VOCAL_RAMP, ramp_out=VOCAL_RAMP))  # fmt: skip
            notes.append(f"Vocals: {quiet.upper()}'s vocal stem is muted over beats {p0:g}-{p1:g}.")
    return forces, notes


def _kick_floors_needed(plan: MixPlan, forces: list[Force], a: Track, b: Track) -> list[Force]:
    """The forces without the high-passes a deck doesn't need: one without stems has its lows killed by its folded
    bass lane wherever the high-pass would go, and that already keeps its kick out."""
    stems_planned = apply_forces(plan, [f for f in forces if f.kind != "floor"])
    playing = fold_stem_lanes(stems_planned, lambda track: (a if track == plan.a.track_id else b).has_stems)
    keep = []
    for f in forces:
        if f.kind == "floor":
            beats = np.arange(f.start, f.end, STEP)
            lows = np.asarray(lane_value(getattr(playing, f.deck).lanes.eq_low, beats), float)
            if beats.size and np.all(lows <= KICK_KILLED_DB):
                continue
        keep.append(f)
    return keep


def plan(generated: MixPlan, a: Track, b: Track) -> tuple[MixPlan, dict]:
    """The generated plan with stem-aware hand-offs, and a report (`forces`, `bass_beat`, `mashup`, `checks`). Edited
    plans and cuts come back unchanged."""
    if generated.edited:
        return generated, {"skipped": "edited"}
    mashup = find_mashup(generated, a, b) if a.has_stems or b.has_stems else None
    out = generated if mashup is None else shift_b(generated, mashup["shift"])
    assert out is not None
    span = blend(out)
    if span is None:
        return generated, {"skipped": "cut", "checks": check(generated, a, b)}
    hand = mashup["bass_beat"] if mashup else bass_beat(out, b, span)
    notes: list[str] = []
    decks = {"a": out.a, "b": out.b}
    for name, track in (("a", a), ("b", b)):
        if decks[name].stem_lanes is None:
            decks[name] = decks[name].model_copy(update={"stem_lanes": _flat_stems()})
        if track.has_stems:
            decks[name], dropped = _no_duck(decks[name])
            if dropped:
                notes.append(f"Deck {name.upper()}'s mid duck is gone: the vocal stem is muted instead.")
    out = out.model_copy(update=decks)
    far = out.window.end_beat
    first = out.window.start_beat
    forces: list[Force] = []
    if a.has_stems or b.has_stems:
        forces += [Force("a", "gain_bass", "mute", hand, far, ramp_in=BASS_RAMP),
                   Force("b", "gain_bass", "mute", first, hand, ramp_out=BASS_RAMP)]  # fmt: skip
        opens = _bass_in(out, (hand, far))
        if opens > hand:  # the generator's lows open after the hand-off: they open on it
            forces.append(Force("b", "eq_low", "floor", hand, opens, value=0.0))
        leaves = _lows_out(out.a, (span[0], hand))
        if a.has_stems and leaves < hand:  # ... and A's carry to it, so that there's no hole between them
            forces.append(Force("a", "eq_low", "floor", leaves, hand - BASS_RAMP, value=0.0))
        notes.append(f"Bass hand-off on beat {hand:g}: A's bass stem out, B's in, never both.")
    else:
        notes.append("Bass: neither deck has stems, the planned EQ swap stays.")
    a_end = min(out.a.play_to_beat, far)
    if mashup and mashup["direction"] == "forward":
        forces += [Force("b", lane, "mute", first, hand, ramp_out=BASS_RAMP) for lane in ("gain_drums", "gain_other")]
        forces.append(Force("b", "gain_vocals", "mute", first, mashup["start"], ramp_out=VOCAL_RAMP))
        forces.append(Force("a", "gain_vocals", "mute", mashup["start"], a_end, ramp_in=VOCAL_RAMP))
        forces.append(Force("a", "filter", "floor", hand, a_end, value=KICK_FILTER))
    elif mashup:
        forces += [Force("a", lane, "mute", hand, far, ramp_in=BASS_RAMP) for lane in ("gain_drums", "gain_other")]
        forces.append(Force("a", "gain_vocals", "mute", mashup["end"], far, ramp_in=VOCAL_RAMP))
        forces.append(Force("b", "gain_vocals", "mute", first, mashup["end"], ramp_out=VOCAL_RAMP))
        forces.append(Force("b", "filter", "floor", span[0], hand, value=KICK_FILTER))
    else:
        forces.append(Force("b", "filter", "floor", span[0], hand, value=KICK_FILTER))
        forces.append(Force("a", "filter", "floor", hand, max(a_end, hand + STEP), value=KICK_FILTER))
    if mashup:
        voice, music = ("B", "A") if mashup["direction"] == "forward" else ("A", "B")
        moved = f", B moved {mashup['shift']:+g} beats to line it up" if mashup["shift"] else ""
        notes.append(f"Mashup: {voice}'s acapella over {music}'s instrumental for {mashup['bars']} bars "
                     f"(beats {mashup['start']:g}-{mashup['end']:g}){moved}.")  # fmt: skip
    else:
        notes.append(f"Drums: B's come in under a {KICK_HP_HZ:g} Hz high-pass up to the hand-off, A's go out under it.")
        mutes, vnotes = _vocal_mutes(out, a, b, span, hand)
        forces += mutes
        notes += vnotes
    forces = _kick_floors_needed(out, [f for f in forces if f.end > f.start], a, b)
    out = apply_forces(out, forces)
    out = out.model_copy(update={"notes": [*out.notes, *notes][-_MAX_NOTES:]})
    report = {"bass_beat": hand, "blend": [span[0], span[1]], "mashup": mashup, "forces": [f.to_json() for f in forces],
              "checks": check(out, a, b)}  # fmt: skip
    return out, report


# --- checks ----------------------------------------------------------------------------------------------------------


def _db(x: float) -> float:
    return round(10.0 * math.log10(x), 1) if x > 1e-12 else _SILENT_DB


def check(plan: MixPlan, a: Track, b: Track) -> dict:
    """The rules measured on the plan as it plays on these tracks (stem lanes folded into the EQ of a deck without
    stems): `bass_overlap_db`, the bass both decks play at once against the larger of the two, in dB;
    `vocal_overlap_s`, seconds two vocals are heard together (None when either deck's vocals aren't known);
    `kick_double_s`, seconds two kicks are. `ok` when each is within LIMITS."""
    playing = fold_stem_lanes(plan, lambda track: (a if track == plan.a.track_id else b).has_stems)
    beats = _sample(playing)
    sides = [_Side(playing.a, a, beats), _Side(playing.b, b, beats)]
    spb = 60.0 / np.asarray(lane_value(playing.tempo, beats), float)
    dt = spb * STEP
    bass, voice, kick = [], [], []
    for s in sides:
        low = s.volume * s.lane("gain_bass") * 10 ** (s.lane("eq_low") / 20.0) * s.hp_gain(BASS_HZ)
        bass.append((low**2) * s.activity("bass"))
        mid = s.volume * s.lane("gain_vocals") * 10 ** (np.minimum(s.lane("eq_mid"), 0.0) / 20.0)
        voice.append(s.sung() & (mid >= VOCAL_HEARD))
        k = s.volume * s.lane("gain_drums") * s.activity("drums") * 10 ** (s.lane("eq_low") / 20.0) * s.hp_gain(KICK_HZ)
        kick.append(k >= KICK_HEARD)
    shared, larger = float(np.sum(np.minimum(*bass) * dt)), float(np.sum(np.maximum(*bass) * dt))
    out: dict = {
        "bass_overlap_db": _db(shared / larger) if larger > 0 else _SILENT_DB,
        "vocal_overlap_s": (None if a.vocals is None or b.vocals is None
                            else round(float(np.sum((voice[0] & voice[1]) * dt)), 2)),
        "kick_double_s": round(float(np.sum((kick[0] & kick[1]) * dt)), 2),
    }  # fmt: skip
    out["ok"] = all(out[k] is None or out[k] <= limit for k, limit in LIMITS.items())
    return out
