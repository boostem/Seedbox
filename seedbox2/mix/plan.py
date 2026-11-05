"""Mix plan v3: the automation script of one A -> B transition, as JSON, for the offline renderer and the live engine.

Everything lives on one master timeline in beats, as in Seedbox 1 (`musicreco/mixplan.py`): beat 0 is where B comes
in, and lanes, loops, effects and the tempo map are keyed in master beats. What is new:

- Every deck has a piecewise `grid` `[{start_beat, start_s, bpm}]`, the shape of `seedbox2.analysis.grid`, numbered in
  master beats. A constant-tempo deck has one segment. `deck_time` is the one way from a master beat to file time.
  To place a track's analysed grid on the master timeline use `Grid.scaled(factor, offset).to_json()`: `offset` is the
  track's beat that sounds at master beat 0, `factor` 2 or 0.5 for a half- or double-time mix.
- A deck can have `stem_lanes`: linear gains, so 0 is true silence. `null` means the deck plays the full mix. The
  stored plan keeps its stem lanes; `validate(raw, has_stems=...)` hands the engines a plan whose decks without stems
  play those lanes on their EQ instead (`fold_stem_lanes`).
- `key_shift`: whole semitones per deck, on top of `key_lock`.

Validation refuses what's wrong instead of clamping it: an editor should hear about a bad value, and only the
converter from Seedbox 1 (`convert.py`) repairs anything. A `ValueError` (pydantic's `ValidationError` is one) means
it isn't a plan. The TypeScript types in `frontend2` are generated from these models (`typescript.py`).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Annotated, Literal

import numpy as np
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from ..analysis.grid import Grid

PLAN_VERSION = 3  # Seedbox 1's plans are version 2

LANES = (
    "volume", "eq_low", "eq_mid", "eq_high", "filter", "filter_res", "echo_send", "echo_feedback", "reverb_send",
)  # fmt: skip
STEM_LANES = ("gain_drums", "gain_bass", "gain_vocals", "gain_other")
LANE_RANGES: dict[str, tuple[float, float]] = {
    "volume": (0.0, 1.0), "eq_low": (-30.0, 6.0), "eq_mid": (-30.0, 6.0), "eq_high": (-30.0, 6.0),
    "filter": (-1.0, 1.0), "filter_res": (0.0, 1.0), "echo_send": (0.0, 1.0), "echo_feedback": (0.0, 0.95),
    "reverb_send": (0.0, 1.0), "gain_drums": (0.0, 1.0), "gain_bass": (0.0, 1.0), "gain_vocals": (0.0, 1.0),
    "gain_other": (0.0, 1.0),
}  # fmt: skip
LANE_DEFAULTS: dict[str, float] = {
    "volume": 1.0, "eq_low": 0.0, "eq_mid": 0.0, "eq_high": 0.0, "filter": 0.0, "filter_res": 0.0,
    "echo_send": 0.0, "echo_feedback": 0.55, "reverb_send": 0.0, "gain_drums": 1.0, "gain_bass": 1.0,
    "gain_vocals": 1.0, "gain_other": 1.0,
}  # fmt: skip
TEMPO_RANGE = (40.0, 300.0)
KEY_SHIFT_MAX = 2  # semitones, either way
SEND_SETTINGS = ("echo_beats", "reverb_size")
PINNABLE = (*LANES, *STEM_LANES, *SEND_SETTINGS)
LOOP_DIVISIONS = (0.5, 0.25, 0.125, 0.0625)  # the lengths under a beat: a roll, still started on a beat
# What a deck without stems does with each stem lane. Drums are broadband and "other" has no band of its own: the top
# of the kit goes with the highs, and "other" is left alone.
STEM_EQ_FALLBACK = {"gain_bass": "eq_low", "gain_vocals": "eq_mid", "gain_drums": "eq_high"}

_MAX_ABS_BEAT = 1_000_000.0  # as far as Seedbox 1 lets a loop or a deck go
_MAX_KEYFRAMES = 4096
_MAX_LOOPS = 64
_MAX_FX = 64
_MAX_FX_BEATS = 16.0
_MAX_SEGMENTS = 512
_MAX_NOTES = 64
_MIN_WINDOW_BEATS = 1.0
_MAX_WINDOW_BEATS = 512.0
_GRID_GAP_S = 0.005  # segments must meet this closely (grids in JSON are rounded)
_ON_BEAT = 1e-6
_FOLD_STEP = 0.25  # beats between the keyframes of a stem lane folded into EQ, while something moves
_FOLD_MAX_POINTS = 1024  # ... coarser over spans that would take more than this
_LEFT = 1e-6  # how far before a beat to look for the value a lane arrives with
_SAME_DB = 1e-3


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False, json_schema_serialization_defaults_required=True,
    )  # fmt: skip


Beat = Annotated[float, Field(ge=-_MAX_ABS_BEAT, le=_MAX_ABS_BEAT)]


class Keyframe(_Model):
    """A lane's value at a master beat. The shape up to the next keyframe is this one's curve: linear, ease (smoothstep)
    or hold. Two keyframes on one beat are an instant step."""

    beat: Beat
    value: float
    curve: Literal["linear", "hold", "ease"] = "linear"


def _sorted(keyframes: list[Keyframe]) -> list[Keyframe]:
    return sorted(keyframes, key=lambda k: k.beat)  # stable: a step keeps its order


Lane = Annotated[list[Keyframe], Field(min_length=1, max_length=_MAX_KEYFRAMES), AfterValidator(_sorted)]


def _check_ranges(model: BaseModel, names: tuple[str, ...]) -> None:
    for name in names:
        lo, hi = LANE_RANGES[name]
        for k in getattr(model, name):
            if not lo <= k.value <= hi:
                raise ValueError(f"{name}: {k.value:g} at beat {k.beat:g} is outside {lo:g}..{hi:g}")


class Lanes(_Model):
    """A deck's automation. volume and the sends are 0..1, the EQ bands dB (-30 is a kill), filter -1..1 (low-pass below
    0, high-pass above)."""

    volume: Lane
    eq_low: Lane
    eq_mid: Lane
    eq_high: Lane
    filter: Lane
    filter_res: Lane
    echo_send: Lane
    echo_feedback: Lane
    reverb_send: Lane

    @model_validator(mode="after")
    def _in_range(self) -> Lanes:
        _check_ranges(self, LANES)
        return self


class StemLanes(_Model):
    """Linear gain of each stem, 0..1: 0 is true silence, 1 the stem as separated."""

    gain_drums: Lane
    gain_bass: Lane
    gain_vocals: Lane
    gain_other: Lane

    @model_validator(mode="after")
    def _in_range(self) -> StemLanes:
        _check_ranges(self, STEM_LANES)
        return self


class GridSegment(_Model):
    """From `start_beat` (master beats) up to the next segment's: the deck runs at `bpm` from `start_s` in the file."""

    start_beat: Beat
    start_s: float
    bpm: float = Field(ge=20.0, le=400.0)


class Loop(_Model):
    """Slip loop: over [start_beat, end_beat) the deck plays source_beat + ((beat - start_beat) mod length_beats), and
    on release it is where it would have been. All on whole beats; `length_beats` may also be 1/2, 1/4, 1/8 or 1/16."""

    start_beat: Beat
    end_beat: Beat
    source_beat: Beat
    length_beats: float = Field(gt=0.0, le=256.0)

    @model_validator(mode="after")
    def _on_beats(self) -> Loop:
        if self.end_beat <= self.start_beat:
            raise ValueError("a loop's end_beat must be after its start_beat")
        for name in ("start_beat", "end_beat", "source_beat"):
            if not on_beat(getattr(self, name)):
                raise ValueError(f"a loop's {name} must be on a whole beat, not {getattr(self, name):g}")
        if not loop_length_ok(self.length_beats):
            raise ValueError(f"a loop's length must be whole beats or 1/2, 1/4, 1/8, 1/16, not {self.length_beats:g}")
        return self


class _FxBase(_Model):
    start_beat: Beat
    end_beat: Beat

    @model_validator(mode="after")
    def _span(self) -> _FxBase:
        if not 0.0 < self.end_beat - self.start_beat <= _MAX_FX_BEATS:
            raise ValueError(f"an effect's end_beat must be after its start_beat, at most {_MAX_FX_BEATS:g} beats")
        return self


class StopFx(_FxBase):
    """The record stops over the span and the deck is silent after it: pushed back (spinback) or braked."""

    type: Literal["spinback", "brake"]


class StutterFx(_FxBase):
    """A gated retrigger of the slice at start_beat, repeated every `div` beats."""

    type: Literal["stutter"]
    div: float = Field(default=0.25, ge=0.0625, le=1.0)


Fx = Annotated[StopFx | StutterFx, Field(discriminator="type")]


class Sends(_Model):
    """The settings of a deck's echo and reverb. A send taps the deck before its EQ, filter and fader, or after them."""

    echo_beats: float = Field(default=0.75, ge=0.125, le=4.0)
    echo_tap: Literal["pre_eq", "post_fader"] = "pre_eq"
    reverb_size: float = Field(default=0.75, ge=0.0, le=1.0)
    reverb_tap: Literal["pre_eq", "post_fader"] = "pre_eq"


def _no_overlap(events: list) -> list:
    ordered = sorted(events, key=lambda e: e.start_beat)
    for p, q in zip(ordered, ordered[1:]):
        if q.start_beat < p.end_beat - 1e-9:
            raise ValueError(f"loops or effects of one deck overlap at beat {q.start_beat:g}")
    return ordered


class Deck(_Model):
    """One track in the mix. The master position maps to file time through `grid`, sorted and continuous; its first
    segment also holds before its start, the last one after it. Times outside the file are silence."""

    track_id: str = Field(min_length=1)
    grid: list[GridSegment] = Field(min_length=1, max_length=_MAX_SEGMENTS)
    key_shift: int = Field(default=0, ge=-KEY_SHIFT_MAX, le=KEY_SHIFT_MAX, description="Semitones.")
    play_from_beat: Beat
    play_to_beat: Beat
    lanes: Lanes
    stem_lanes: StemLanes | None = Field(default=None, description="Null: the deck plays the full mix.")
    sends: Sends = Sends()
    loops: Annotated[list[Loop], Field(max_length=_MAX_LOOPS), AfterValidator(_no_overlap)] = []
    fx: Annotated[list[Fx], Field(max_length=_MAX_FX), AfterValidator(_no_overlap)] = []
    pinned: list[str] = Field(default=[], max_length=len(PINNABLE), description="Set by a person: never regenerated.")

    @model_validator(mode="after")
    def _consistent(self) -> Deck:
        for name in self.pinned:
            if name not in PINNABLE:
                raise ValueError(f"pinned: {name!r} isn't a lane or a send setting")
        if self.play_from_beat >= self.play_to_beat:
            raise ValueError("play_from_beat must be before play_to_beat")
        for a, b in zip(self.grid, self.grid[1:]):
            if b.start_beat <= a.start_beat:
                raise ValueError("grid segments must be sorted by start_beat, on different beats")
            if abs(a.start_s + (b.start_beat - a.start_beat) * 60.0 / a.bpm - b.start_s) > _GRID_GAP_S:
                raise ValueError(f"grid is not continuous at beat {b.start_beat:g}")
        return self


class Window(_Model):
    """The master beats a render or preview covers."""

    start_beat: Beat
    end_beat: Beat


class Marker(_Model):
    beat: float
    label: str = Field(min_length=1, max_length=40)


class MixPlan(_Model):
    """One A -> B transition. `bars` is the blend length; for a cut it is the length of A's lead-in effect before the
    cut. `edited`: a person changed it, so nothing may regenerate it."""

    version: Literal[3] = PLAN_VERSION
    technique: str = Field(min_length=1)
    bars: int = Field(ge=1, le=128)
    key_lock: bool = True
    window: Window
    tempo: Lane = Field(description="The master tempo map: BPM keyframes in master beats.")
    a: Deck
    b: Deck
    markers: Annotated[list[Marker], AfterValidator(lambda ms: sorted(ms, key=lambda m: m.beat))] = []
    notes: list[str] = Field(default=[], max_length=_MAX_NOTES)
    edited: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> MixPlan:
        for k in self.tempo:
            if not TEMPO_RANGE[0] <= k.value <= TEMPO_RANGE[1]:
                raise ValueError(f"tempo: {k.value:g} BPM at beat {k.beat:g} is out of range")
        w = self.window
        if w.start_beat < min(self.a.play_from_beat, self.b.play_from_beat) or (
            w.end_beat > max(self.a.play_to_beat, self.b.play_to_beat)
        ):
            raise ValueError("the window is outside the decks' play range")
        if not _MIN_WINDOW_BEATS <= w.end_beat - w.start_beat <= _MAX_WINDOW_BEATS:
            raise ValueError(f"the window must be {_MIN_WINDOW_BEATS:g} to {_MAX_WINDOW_BEATS:g} beats long")
        return self


def on_beat(x: float) -> bool:
    return abs(x - round(x)) < _ON_BEAT


def loop_length_ok(length: float) -> bool:
    return (length >= 1.0 and on_beat(length)) or any(abs(length - d) < _ON_BEAT for d in LOOP_DIVISIONS)


# --- the shared maths: what both engines execute ---------------------------------------------------------------------


def lane_value(keyframes: list[Keyframe], beat: float | np.ndarray) -> float | np.ndarray:
    """A lane's value at master beat(s). Constant before the first and after the last keyframe; two keyframes on one
    beat are a step (the later one wins from that beat on)."""
    beats = np.array([k.beat for k in keyframes])
    vals = np.array([k.value for k in keyframes])
    code = np.array([0 if k.curve == "hold" else 2 if k.curve == "ease" else 1 for k in keyframes])
    x = np.asarray(beat, dtype=float)
    i = np.clip(np.searchsorted(beats, x, side="right") - 1, 0, len(keyframes) - 1)
    j = np.minimum(i + 1, len(keyframes) - 1)
    span = beats[j] - beats[i]
    t = np.clip(np.where(span > 0, (x - beats[i]) / np.where(span > 0, span, 1.0), 0.0), 0.0, 1.0)
    shape = np.where(code[i] == 0, 0.0, np.where(code[i] == 2, t * t * (3 - 2 * t), t))
    out = np.where(x < beats[0], vals[0], vals[i] + (vals[j] - vals[i]) * shape)
    return float(out) if np.ndim(out) == 0 else out


def deck_position(deck: Deck, beat: float | np.ndarray) -> float | np.ndarray:
    """Master beat(s) -> position on the deck's grid: the beat itself, or inside a loop
    source_beat + ((beat - start_beat) mod length_beats)."""
    x = np.asarray(beat, dtype=float)
    u = x.copy()
    for lp in deck.loops:
        inside = (x >= lp.start_beat) & (x < lp.end_beat)
        u = np.where(inside, lp.source_beat + np.mod(x - lp.start_beat, lp.length_beats), u)
    return float(u) if np.ndim(u) == 0 else u


def deck_grid(deck: Deck) -> Grid:
    """The deck's grid, for many lookups (a renderer keeps it rather than rebuild it per call)."""
    return Grid(s.model_dump() for s in deck.grid)


def deck_time(deck: Deck, beat: float | np.ndarray) -> float | np.ndarray:
    """File time (s) of a deck at master beat(s), each in the grid segment of its position."""
    return deck_grid(deck).time_at(deck_position(deck, beat))


def deck_bpm(deck: Deck, beat: float | np.ndarray) -> float | np.ndarray:
    """The deck's own tempo at master beat(s): the local tempo of its grid, which sets the stretch ratio."""
    return deck_grid(deck).bpm_at(deck_position(deck, beat))


# --- validation ------------------------------------------------------------------------------------------------------


def _gain_db(gain: float | np.ndarray) -> float | np.ndarray:
    return 20.0 * np.log10(np.maximum(gain, 1e-6))


def _fold_points(eq: list[Keyframe], gain: list[Keyframe]) -> list[tuple[float, bool]]:
    """The beats of both lanes, plus samples where something moves, each with whether the span after it moves."""
    knots = sorted({k.beat for k in eq} | {k.beat for k in gain})
    moving = []
    for b0, b1 in zip(knots, knots[1:]):
        moves = any(abs(float(lane_value(kf, b0)) - float(lane_value(kf, b1 - _LEFT))) > 1e-9 for kf in (eq, gain))
        moving.append(moves)
    total = sum(b1 - b0 for b0, b1, m in zip(knots, knots[1:], moving) if m)
    step = max(_FOLD_STEP, total / _FOLD_MAX_POINTS)
    points = []
    for b0, b1, m in zip(knots, knots[1:], moving):
        n = math.ceil((b1 - b0) / step) if m else 1
        points += [(b0 + (b1 - b0) * i / n, m) for i in range(n)]
    return [*points, (knots[-1], False)]


def _fold_lane(eq: list[Keyframe], gain: list[Keyframe], lo: float, hi: float) -> list[Keyframe]:
    """An EQ lane plus a linear gain lane, as one EQ lane in dB: the shape is kept to within `_FOLD_STEP`, and steps
    stay steps."""

    def db(beat: float) -> float:
        return float(np.clip(lane_value(eq, beat) + _gain_db(lane_value(gain, beat)), lo, hi))

    out: list[Keyframe] = []
    for beat, moves in _fold_points(eq, gain):
        arrive, value = db(beat - _LEFT), db(beat)
        if out and out[-1].curve != "hold" and abs(arrive - value) > _SAME_DB:
            out.append(Keyframe(beat=beat, value=arrive, curve="linear"))
        out.append(Keyframe(beat=beat, value=value, curve="linear" if moves else "hold"))
    return out


def _fold_deck(deck: Deck) -> Deck:
    stems, lanes = deck.stem_lanes, deck.lanes.model_dump()
    assert stems is not None
    for stem, band in STEM_EQ_FALLBACK.items():
        gain = getattr(stems, stem)
        if any(k.value != 1.0 for k in gain):
            lanes[band] = [k.model_dump() for k in _fold_lane(getattr(deck.lanes, band), gain, *LANE_RANGES[band])]
    return deck.model_copy(update={"lanes": Lanes.model_validate(lanes), "stem_lanes": None})


def fold_stem_lanes(plan: MixPlan, has_stems: Callable[[str], bool]) -> MixPlan:
    """The plan as it plays on these tracks: a deck whose track has no stems gets its stem lanes on its EQ instead (bass
    on the lows, vocals on the mids, drums on the highs; "other" has no band, so it stays up). A silent stem becomes
    an EQ kill, which is -30 dB and not silence. Says so in the notes when it changes what the deck does."""
    update: dict = {}
    notes = list(plan.notes)
    for name in ("a", "b"):
        deck = getattr(plan, name)
        if deck.stem_lanes is None or has_stems(deck.track_id):
            continue
        update[name] = _fold_deck(deck)
        if any(k.value != 1.0 for lane in STEM_LANES for k in getattr(deck.stem_lanes, lane)):
            notes.append(f"Deck {name.upper()} has no stems: its stem lanes play on its EQ.")
    return plan.model_copy(update={**update, "notes": notes[-_MAX_NOTES:]}) if update else plan


def validate(raw: object, *, has_stems: Callable[[str], bool] | None = None) -> MixPlan:
    """A checked plan from JSON data, or ValueError saying what's wrong. With `has_stems`, the plan as it plays on
    these tracks (see fold_stem_lanes): what engines want. Without it, the plan as stored: what an editor wants."""
    plan = MixPlan.model_validate(raw)
    return plan if has_stems is None else fold_stem_lanes(plan, has_stems)


def json_schema() -> dict:
    """The JSON Schema of what `validate` accepts."""
    return MixPlan.model_json_schema()
