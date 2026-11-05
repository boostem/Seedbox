"""A whole set as one bus, a chunk at a time (#70): what `stream.py` and `stretch.py` do for a deck, walked over the set.

`SetRender.step` gives the next `CHUNK` samples of the set before its master chain. Nothing in it holds more than a block
of audio per deck:

- A `_Segment` is one plan, from where it takes its track over to where the next plan takes it (`setchain`). A plan's deck
  is a `_Strip`, which runs what `render._mix_deck` runs, with its state kept in it.
- A `_Blocks` decodes and stretches a strip's audio `BLOCK_BEATS` at a time, cut on a master beat, away from deck effects.
  The block's seam is a linear crossfade of `stretch.SEAM_S` ending on the cut: both sides are the same place in the same
  file, so they add up. A deck at its own tempo is copied, and its seams cross-fade identical samples.
- A deck with stems plays them only in the blocks where a stem lane isn't at 1, which saves stretching four files for the
  whole body of each track. Stems add up to about the mix, and a block's seam covers what difference there is.
- When a plan takes over, the outgoing and incoming strips are the same place in the same track. Their chains may differ
  (an isolator's phase, a filter's state), so they cross-fade over `HANDOFF_FADE_S`. Echo and reverb from the outgoing
  segment ring on over the next one for `TAIL_S`, and fade.

`SetRender` is plain data between steps, so it pickles: that's how a render resumes after a crash, from a checkpoint per
track (`checkpoint_due`). The one thing that doesn't pickle is pedalboard's reverb, whose state is inside the plugin. It is
only made when a send first sounds, and checkpoints wait for a moment with none alive: the track's body, after the
last segment's tails have gone and before this one's transition.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from musicreco.mixrender import (
    CTL,
    ECHO_RETURN,
    FILTER_SMOOTH_S,
    REVERB_RETURN,
    STEP_RAMP_S,
    _Clock,
    _deck_fx,
)  # fmt: skip

from . import stream
from . import stretch as stretching
from .plan import STEM_LANES, Deck, MixPlan, deck_grid, deck_position, lane_value
from .setchain import enter_beat, handoff_beat
from .stream import CHUNK, SR

BLOCK_BEATS = 64  # about 30 s: long enough that a seam is rare, short enough that a deck holds little
DECODE_MARGIN_S = 0.3  # decoded around a block, for the stretcher to settle: more than its block plus a seam
FX_CLEARANCE_BEATS = 1.0  # no block is cut this close to a deck effect, which reads the audio around it
HANDOFF_FADE_S = 0.02
TAIL_S = 8.0  # how long a segment's sends ring on past its end
TAIL_FADE_S = 0.5
START_FADE_S, END_FADE_S = 0.005, 0.03  # the set's own ends, as a plan's render has
GATE_IN_S, GATE_OUT_S = 0.003, 0.004


@dataclass(frozen=True)
class Source:
    """A track of the set: its file, its stems if it has them, and the gain that puts its body at DECK_LUFS."""

    hash: str
    name: str
    path: Path
    gain_db: float = 0.0
    stems: dict[str, Path] | None = None


class _Blocks:
    """A deck's audio on its plan's clock, local samples [start, end), in finished blocks."""

    def __init__(self, deck: Deck, source: Source, clock: _Clock, key_lock: bool, start: int, end: int, sr: int) -> None:
        self.deck, self.source, self.clock, self.key_lock, self.sr = deck, source, clock, key_lock, sr
        self.cuts = self._cuts(start, end)
        self.k = 0
        self.seam = round(stretching.SEAM_S * sr)
        self.held: np.ndarray | None = None  # the last seam of the block before: it crossfades into the next

    def _cuts(self, start: int, end: int) -> list[int]:
        """The sample every block starts on, then `end`."""
        spans = [(e.start_beat - (e.end_beat - e.start_beat) - FX_CLEARANCE_BEATS, e.end_beat + FX_CLEARANCE_BEATS)
                 for e in self.deck.fx]  # fmt: skip
        beat = float(self.clock.beat(start / self.sr))
        end_beat = float(self.clock.beat(end / self.sr))
        cuts = [start]
        at = math.floor(beat) + BLOCK_BEATS
        while at < end_beat:
            while any(lo < at < hi for lo, hi in spans):
                at += 1.0
            sample = round(float(self.clock.time(at)) * self.sr)
            if cuts[-1] < sample < end:
                cuts.append(sample)
            at += BLOCK_BEATS
        return [*cuts, end]

    @property
    def done(self) -> bool:
        return self.k >= len(self.cuts) - 1 and self.held is None

    def _stems_at(self, beats: np.ndarray) -> bool:
        lanes = self.deck.stem_lanes
        if lanes is None or not self.source.stems:
            return False
        ends = (float(beats[0]) - 1.0, float(beats[-1]) + 1.0)
        for name in STEM_LANES:
            lane = getattr(lanes, name)
            inside = [k.value for k in lane if ends[0] <= k.beat <= ends[1]]
            if any(v != 1.0 for v in [*inside, *(float(lane_value(lane, e)) for e in ends)]):
                return True
        return False

    def _read(self, path: Path, pos: np.ndarray, beats: np.ndarray, profile: stretching.Profile) -> np.ndarray:
        margin = round(DECODE_MARGIN_S * self.sr)
        lo = math.floor(float(pos.min())) - margin
        hi = math.ceil(float(pos.max())) + margin
        src = stream._read_window(path, lo, hi - lo, self.sr)[0]
        shift = self.deck.key_shift
        if self.key_lock:
            return stretching.stretch(src, pos - lo, beats, self.sr, semitones=shift, profile=profile)
        if shift:  # shift first, at the file's own speed, as `stretch.render_deck`
            here = np.arange(src.shape[1], dtype=np.float64)
            src = stretching.stretch(src, here, here / self.sr, self.sr, semitones=shift, profile=profile)
        return stretching._resample(src, pos - lo)

    def _render(self, a: int, b: int) -> np.ndarray:
        beats = self.clock.beat(np.arange(a, b, dtype=np.float64) / self.sr)
        pos = deck_grid(self.deck).time_at(deck_position(self.deck, beats)) * self.sr
        if not self._stems_at(beats):
            return self._read(self.source.path, pos, beats, stretching.FULL)
        out = np.zeros((2, b - a), np.float32)
        at = np.arange(a, b + CTL, CTL, dtype=np.float64)
        ctl_beats = self.clock.beat(at / self.sr)
        for name in STEM_LANES:
            stem = name.removeprefix("gain_")
            lane = stream.Lane(lane_value(getattr(self.deck.stem_lanes, name), ctl_beats), CTL, STEP_RAMP_S, self.sr)
            y = self._read(self.source.stems[stem], pos, beats, stretching.STEM_PROFILES.get(stem, stretching.FULL))
            out += y * lane.at(0, b - a)
        return out

    def next(self) -> tuple[int, np.ndarray]:
        """(its first local sample, the next finished block)."""
        if self.k >= len(self.cuts) - 1:
            out, self.held = self.held, None
            return self.cuts[-1] - out.shape[1], out
        a, b = self.cuts[self.k], self.cuts[self.k + 1]
        first = self.held is None
        lead = 0 if first else min(self.seam, self.held.shape[1])
        y = self._render(a - lead, b)
        self.k += 1
        if not first:
            fade = ((np.arange(lead) + 0.5) / lead).astype(np.float32)
            y[:, :lead] = self.held[:, -lead:] * (1 - fade) + y[:, :lead] * fade
        keep = min(self.seam, y.shape[1] - lead)
        self.held = y[:, y.shape[1] - keep :].copy()
        return a - lead, y[:, : y.shape[1] - keep]


class _Send:
    def __init__(self, name: str, lane: stream.Lane, tap: str) -> None:
        self.name, self.lane, self.tap = name, lane, tap
        self.effect = None  # made when it first sounds


class _Strip:
    """A deck of a segment: gate, gain and effects a block at a time, then EQ, filter, fader and sends a chunk at a time."""

    def __init__(self, plan: MixPlan, deck: Deck, source: Source, clock: _Clock, start: int, end: int, sr: int) -> None:
        self.deck, self.clock, self.sr = deck, clock, sr
        w0, w1 = plan.window.start_beat, plan.window.end_beat
        p0, p1 = max(deck.play_from_beat, w0), min(deck.play_to_beat, w1)
        self.t0, self.t1 = float(clock.time(p0)) * sr, float(clock.time(p1)) * sr
        self.fade_in = round(GATE_IN_S * sr) if p0 > w0 else 0
        self.fade_out = round(GATE_OUT_S * sr) if p1 < w1 else 0
        self.qa = max(start, math.floor(self.t0) - self.fade_in)
        self.qb = min(end, math.ceil(self.t1) + 1)
        self.playing = p1 > p0 and self.qb > self.qa
        self.gain = 10 ** (source.gain_db / 20)
        self.events = [e.model_dump() for e in deck.fx]
        self.stopped = False
        self.blocks = _Blocks(deck, source, clock, plan.key_lock, self.qa, self.qb, sr) if self.playing else None
        self.buf = np.zeros((2, 0), np.float32)
        self.buf_at = self.qa
        self.sends: list[_Send] = []
        self.iso = self.dj = None
        if self.playing:
            self._mixer()

    def _lane(self, name: str, smooth: float = STEP_RAMP_S) -> stream.Lane:
        at = (self.qa + np.arange(0, self.qb - self.qa + CTL, CTL)) / self.sr
        return stream.Lane(lane_value(getattr(self.deck.lanes, name), self.clock.beat(at)), CTL, smooth, self.sr)

    def _mixer(self) -> None:
        lanes = self.deck.lanes
        self.volume = self._lane("volume")
        if any(k.value != 0 for lane in (lanes.eq_low, lanes.eq_mid, lanes.eq_high) for k in lane):
            self.iso = stream.Isolator(self.sr), self._lane("eq_low"), self._lane("eq_mid"), self._lane("eq_high")
        if any(k.value != 0 for k in lanes.filter):
            res = self._lane("filter_res", FILTER_SMOOTH_S) if any(k.value > 0 for k in lanes.filter_res) else None
            self.dj = stream.DjFilter(self._lane("filter", FILTER_SMOOTH_S), res, self.qb - self.qa, self.sr)
        if any(k.value > 0 for k in lanes.echo_send):
            self.fb = self._lane("echo_feedback")
            self.sends.append(_Send("echo", self._lane("echo_send"), self.deck.sends.echo_tap))
        if any(k.value > 0 for k in lanes.reverb_send):
            self.sends.append(_Send("reverb", self._lane("reverb_send"), self.deck.sends.reverb_tap))

    @property
    def reverb_alive(self) -> bool:
        return any(s.name == "reverb" and s.effect is not None for s in self.sends)

    @property
    def ringing(self) -> bool:
        return any(s.effect is not None for s in self.sends)

    def _start_send(self, s: _Send) -> None:
        if s.name == "reverb":
            s.effect = stream.Reverb(self.deck.sends.reverb_size, self.sr)
            return
        last = self.deck.play_to_beat - 1.0 / 64
        # repeats keep the deck's tempo once it stops: a tempo jump on the cut would make the delay line jump
        slowest = float(self.clock.bpm(np.minimum(self.clock.grid, last)).min())
        s.effect = stream.Echo(self.deck.sends.echo_beats * 60.0 / slowest * self.sr, self.sr)

    def _delay(self, a: int, b: int) -> np.ndarray:
        beats = np.minimum(self.clock.beat(np.arange(a, b) / self.sr), self.deck.play_to_beat - 1.0 / 64)
        return self.deck.sends.echo_beats * 60.0 / self.clock.bpm(beats) * self.sr

    def _gated(self, at: int, x: np.ndarray) -> np.ndarray:
        q = np.arange(at, at + x.shape[1], dtype=np.float64)
        g = np.ones(x.shape[1], np.float32)
        if self.fade_in:
            g = np.minimum(g, np.clip((q - (self.t0 - self.fade_in)) / self.fade_in, 0, 1).astype(np.float32))
        g = np.minimum(g, np.clip((self.t1 - q) / max(self.fade_out, 1), 0, 1).astype(np.float32))
        return x * (np.sin(0.5 * np.pi * g) ** 2).astype(np.float32) * np.float32(self.gain)

    def _advance(self) -> None:
        at, x = self.blocks.next()
        if self.stopped:
            x = np.zeros_like(x)
        else:
            x = self._gated(at, x)
            end = at + x.shape[1]
            events = []
            for e in self.events:
                ts, te = (float(self.clock.time(e[k])) * self.sr for k in ("start_beat", "end_beat"))
                if te > at and ts < end:
                    events.append(e)
                    self.stopped = self.stopped or e["type"] != "stutter"
            if events:
                x = _deck_fx(x, events, self.clock, at, self.sr)
        self.buf = np.concatenate([self.buf, x], axis=1)  # the blocks follow on from each other

    def _take(self, a: int, b: int) -> np.ndarray:
        """Local samples [a, b), after gate, gain and effects."""
        while self.buf_at + self.buf.shape[1] < b and not self.blocks.done:
            # what's behind `a` goes first, so that a deck holds one block
            drop = a - self.buf_at
            if drop > 0:
                self.buf, self.buf_at = self.buf[:, drop:], a
            self._advance()
        out = np.zeros((2, b - a), np.float32)
        i, j = a - self.buf_at, min(b - self.buf_at, self.buf.shape[1])
        if j > i:
            out[:, : j - i] = self.buf[:, i:j]
        return out

    def mix(self, dry: np.ndarray, fx: np.ndarray, s0: int, s1: int, dry_gain: np.ndarray | None) -> None:
        """Adds local samples [s0, s1) to the buses. `dry_gain` shapes what the handoff cross-fades, per sample of them."""
        if not self.playing:
            return
        a, inside = max(s0, self.qa), min(s1, self.qb)
        c = y = None
        if a < inside:
            c = self._take(a, inside)
            y = c
            ra, rb = a - self.qa, inside - self.qa
            if self.iso is not None:
                gains = [stream.iso_gain(np.float32(b.flat)) if b.flat is not None else stream.iso_gain(b.at(ra, rb))
                         for b in self.iso[1:]]  # fmt: skip
                y = self.iso[0](y, *gains)
            if self.dj is not None:
                y = self.dj(y, ra)
            y = y * self.volume.at(ra, rb)
            dry[:, a - s0 : inside - s0] += y if dry_gain is None else y * dry_gain[a - s0 : inside - s0]
        self._sends(fx, s0, s1, a, inside, c, y)

    def _sends(self, fx: np.ndarray, s0: int, s1: int, a: int, inside: int, c: np.ndarray | None, y: np.ndarray | None) -> None:
        from_at = max(s0, self.qa)
        if from_at >= s1:
            return
        for s in self.sends:
            start = 0
            send = np.zeros((2, s1 - from_at), np.float32)
            if c is not None:
                level = s.lane.at(a - self.qa, inside - self.qa)
                if s.effect is None:
                    loud = level > 1e-4
                    if not loud.any():
                        continue
                    start = int(np.argmax(loud))
                    self._start_send(s)
                send[:, start : inside - from_at] = (c if s.tap == "pre_eq" else y)[:, start:] * level[start:]
            elif s.effect is None:
                continue
            at0 = from_at + start
            if s.name == "echo":
                k = s1 - at0
                feedback = np.full(k, self.fb.v[-1], np.float32)
                if c is not None and inside > at0:
                    feedback[: inside - at0] = self.fb.at(at0 - self.qa, inside - self.qa)
                ret = s.effect(send[:, start:], self._delay(at0, s1), feedback) * np.float32(ECHO_RETURN)
            else:
                ret = s.effect(send[:, start:]) * np.float32(REVERB_RETURN)
            fx[:, at0 - s0 : s1 - s0] += ret


class _Segment:
    """One plan's stretch of the set: from sample `start` of the set, `length` samples up to where the next plan takes over."""

    def __init__(self, plan: MixPlan, enter: float, leave: float, a: Source, b: Source, start: int, sr: int, *,
                 first: bool, last: bool) -> None:  # fmt: skip
        self.sr, self.start, self.first, self.last = sr, start, first, last
        self.clock = make_clock(plan, enter, sr)
        self.s0 = round(float(self.clock.time(enter)) * sr)
        self.s_end = round(float(self.clock.time(leave)) * sr)
        self.length = self.s_end - self.s0
        self.fade = 0 if last else round(HANDOFF_FADE_S * sr)
        dry_end = self.s_end + self.fade
        self.strips = [_Strip(plan, plan.a, a, self.clock, self.s0, dry_end, sr), _Strip(plan, plan.b, b, self.clock, self.s0, dry_end, sr)]
        self.tail = 0 if last else round(TAIL_S * sr)
        self.tail_fade = round(TAIL_FADE_S * sr)

    @property
    def end(self) -> int:
        """Where it stops making sound: after the handoff's fade, or once its sends have rung out."""
        ringing = any(s.ringing for s in self.strips)
        return self.start + self.length + (self.tail if ringing else self.fade)

    @property
    def reverb_alive(self) -> bool:
        return any(s.reverb_alive for s in self.strips)

    def _dry_gain(self, s0: int, s1: int) -> np.ndarray | None:
        n = max(round(HANDOFF_FADE_S * self.sr), 1)
        coming = None if self.first or s0 >= self.s0 + n else np.clip((np.arange(s0, s1) + 0.5 - self.s0) / n, 0, 1)
        going = None if self.last or s1 <= self.s_end else np.clip(1 - (np.arange(s0, s1) + 0.5 - self.s_end) / n, 0, 1)
        if coming is None and going is None:
            return None
        ramp = np.ones(s1 - s0)
        for edge in (coming, going):
            if edge is not None:
                ramp = ramp * edge
        return ramp.astype(np.float32)

    def mix(self, dry: np.ndarray, fx: np.ndarray, g0: int) -> None:
        """Adds what it plays in the set's samples [g0, g0 + dry's length) to the buses."""
        n = dry.shape[1]
        s0 = self.s0 + g0 - self.start
        first = max(0, self.s0 - s0)
        s0 += first
        s1 = self.s0 + g0 - self.start + n
        if s1 <= s0:
            return
        gain = self._dry_gain(s0, s1)
        own = np.zeros((2, s1 - s0), np.float32)
        for strip in self.strips:
            strip.mix(dry[:, first:], own, s0, s1, gain)
        over = s1 - (self.s_end + self.tail - self.tail_fade)
        if not self.last and over > 0:  # what's left of the tails fades out
            at = np.arange(s0, s1) - (self.s_end + self.tail - self.tail_fade)
            own *= np.clip(1 - at / self.tail_fade, 0, 1).astype(np.float32)
        fx[:, first:] += own


def make_clock(plan: MixPlan, enter: float, sr: int) -> _Clock:
    tempo = [k.model_dump() for k in plan.tempo]
    w0, w1 = plan.window.start_beat, plan.window.end_beat
    # past the window it holds: sends ring on after it, for at most this many beats at the fastest tempo
    return _Clock(tempo, min(enter, w0) - 3, w1 + TAIL_S * 300 / 60, w0)


def _ranges(chain: list[MixPlan]) -> list[tuple[float, float]]:
    """(enter, leave) on each plan's timeline."""
    out = []
    for i, plan in enumerate(chain):
        enter = enter_beat(plan, chain[i - 1]) if i else plan.window.start_beat
        leave = handoff_beat(plan) if i < len(chain) - 1 else plan.window.end_beat
        out.append((enter, max(leave, enter)))
    return out


@dataclass(frozen=True)
class Layout:
    """Where everything falls in the set, in samples."""

    starts: list[int]  # each segment's first sample
    total: int
    track_starts: list[int]  # where each track comes in


def layout(chain: list[MixPlan], sr: int = SR) -> Layout:
    starts, tracks = [0], [0]
    at = 0
    for plan, (enter, leave) in zip(chain, _ranges(chain)):
        clock = make_clock(plan, enter, sr)
        s0 = round(float(clock.time(enter)) * sr)
        comes_in = max(plan.b.play_from_beat, enter)
        tracks.append(max(tracks[-1], at + round(float(clock.time(comes_in)) * sr) - s0))
        at += round(float(clock.time(leave)) * sr) - s0
        starts.append(at)
    return Layout(starts[:-1], at, tracks)


class SetRender:
    """The set's bus, `step` by `step`. It pickles between steps when `checkpoint_due` says so."""

    def __init__(self, chain: list[MixPlan], sources: list[Source], sr: int = SR) -> None:
        if len(sources) != len(chain) + 1:
            raise ValueError("a set needs a plan between each two tracks")
        self.chain, self.sources, self.sr = chain, sources, sr
        self.ranges = _ranges(chain)
        self.layout = layout(chain, sr)
        self.at = 0
        self.next_segment = 0
        self.active: list[_Segment] = []
        self.checkpointed = 0  # how many segments have had their checkpoint

    @property
    def total(self) -> int:
        return self.layout.total

    @property
    def done(self) -> bool:
        return self.at >= self.total

    @property
    def segment(self) -> int:
        """The segment playing now."""
        return max(self.next_segment - 1, 0)

    def checkpoint_due(self) -> bool:
        """The track's body: this segment alone, and no reverb alive. Once per segment, from the second: before that nothing
        has finished."""
        if self.next_segment < 2 or self.next_segment <= self.checkpointed or len(self.active) != 1:
            return False
        return not self.active[0].reverb_alive

    def checkpointed_now(self) -> None:
        self.checkpointed = self.next_segment

    def _open(self, upto: int) -> None:
        while self.next_segment < len(self.chain) and self.layout.starts[self.next_segment] < upto:
            i = self.next_segment
            enter, leave = self.ranges[i]
            self.active.append(_Segment(self.chain[i], enter, leave, self.sources[i], self.sources[i + 1],
                                        self.layout.starts[i], self.sr, first=i == 0, last=i == len(self.chain) - 1))  # fmt: skip
            self.next_segment += 1

    def step(self) -> np.ndarray:
        """The next chunk of the bus, (2, k) float32."""
        g0, g1 = self.at, min(self.at + CHUNK, self.total)
        self._open(g1)
        dry = np.zeros((2, g1 - g0), np.float32)
        fx = np.zeros((2, g1 - g0), np.float32)
        for seg in self.active:
            seg.mix(dry, fx, g0)
        self.active = [seg for seg in self.active if seg.end > g1 or seg.last]
        self.at = g1
        bus = dry + fx
        fi, fo = round(START_FADE_S * self.sr), round(END_FADE_S * self.sr)
        if g0 < fi:
            k = min(fi - g0, bus.shape[1])
            bus[:, :k] *= ((g0 + np.arange(k)) / fi).astype(np.float32)
        if g1 > self.total - fo:
            over = np.arange(g0, g1) - (self.total - fo)
            bus *= np.clip(1 - over / fo, 0, 1).astype(np.float32)
        return bus
