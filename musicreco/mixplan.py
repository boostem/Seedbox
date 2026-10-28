"""Mix plans (APP_SPEC.md v4): the automation script of one A → B transition, which mixrender turns into audio.

Everything lives on one MASTER timeline in beats. Beat 0 is where B comes in, on A's phrase grid (the placement of
transitions.suggest). Each deck maps master beats to its own file time through its beat grid (deck_time, loops
honoured) and is time-stretched with key lock so its beats land on the master clock, whose BPM follows `tempo`.
Lanes (fader, 3-band isolator EQ, bipolar filter, echo/reverb sends, pre-EQ by default) are keyframed in master
beats, and lane_value is the one interpolation the renderer, the UI and the tests share.

A master bar is 4 master beats: A bar a0 + k and B bar b0 + k sound together in master bar k (a half/double-time B
is put on A's grid first, as in transitions, so its native_bpm is the analysis BPM x 2 or x 0.5). `bars` is the
blend length; for the cuts (slam, quick cut, drop swap, reverb wash, an echo out without overlap) it is the length of
A's lead-in effect (filter riser, reverb swell) before the cut on the 1.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass

import numpy as np

from . import mixing, transitions
from .models import Track
from .structure import phrase_grid

PLAN_VERSION = 2

LANES = ("volume", "eq_low", "eq_mid", "eq_high", "filter", "echo_send", "echo_feedback", "reverb_send")
LANE_RANGES: dict[str, tuple[float, float]] = {
    "volume": (0.0, 1.0), "eq_low": (-30.0, 6.0), "eq_mid": (-30.0, 6.0), "eq_high": (-30.0, 6.0),
    "filter": (-1.0, 1.0), "echo_send": (0.0, 1.0), "echo_feedback": (0.0, 0.95), "reverb_send": (0.0, 1.0),
}  # fmt: skip
ECHO_FEEDBACK = 0.55  # the echo_feedback lane's default, shared with the renderer and the UI
LANE_DEFAULTS: dict[str, float] = {
    "volume": 1.0, "eq_low": 0.0, "eq_mid": 0.0, "eq_high": 0.0, "filter": 0.0,
    "echo_send": 0.0, "echo_feedback": ECHO_FEEDBACK, "reverb_send": 0.0,
}  # fmt: skip
SEND_TAPS = ("pre_eq", "post_fader")  # where a deck's echo / reverb send taps it: before its EQ/filter/fader, or after
REVERB_SIZE = 0.75  # 0..1: room size and decay of a deck's reverb
CURVES = ("linear", "hold", "ease")
TECHNIQUES = (*transitions.TECHNIQUES, "filter sweep", "loop roll", "slam", "reverb wash")
BARS = (4, 8, 16, 32)
TEMPO_RANGE = (40.0, 300.0)
ECHO_BEATS_RANGE = (0.125, 4.0)

_CUTS = ("drop swap", "quick cut", "slam", "reverb wash")
_GOOD_KEYS = ("same", "+1", "-1", "relative")
_SWEEP_KEYS = ("+2 boost", "+7 boost", "clash")  # blends in these keys hide under a filter sweep
_PHRASE = transitions.PHRASE
_KILL = -30.0
_DEFAULT_BPM = 124.0
_SMALL_GAP = 0.02  # tempo gap up to which A plays at its own tempo into the blend and B is held there
_CUT_JUMP_GAP = 0.02  # above this a cut lets B start at its own tempo (the cut hides the change)
_MAX_BLEND_GAP = 0.06  # wider tempo gaps are not blended unless asked: echo out and jump to B's tempo
_RAMP_PER_8 = 0.005  # target tempo drift per 8 bars (imperceptible)
_RAMP_MIN, _RAMP_MAX = 16, 32  # bars
_LOOP_ROLL_GAP = 0.04
_LOOP_VOCAL_MAX = 0.4
_MAX_PREVIEW_S = 92.0
_MAX_WINDOW_BEATS = 512
_MAX_KEYFRAMES = 4096
_MAX_LOOPS = 64
_MAX_ABS_BEAT = 100_000.0
_MIN_LOOP_BEATS = 1 / 16
_VOCAL_CUT = -18.0  # dB on the ducked deck's mids while vocals overlap
_SLAM_INTRO = 8  # B's intro up to this many bars is short enough to slam past
_SLAM_LEAD = 8  # ... when its first drop comes at most this many bars after the intro
_SLAM_JUMP = 1.25  # a drop (B's, or the one A's build leads into) is this much louder than the 4 bars before it
_BUILD_BARS = 32  # A's build counts when the drop it leads into is within A's last 32 bars
_SEND_TAIL = 2.0  # beats A keeps playing (fader down) after it is out, so its echo / reverb sends still get audio
_MID_THROWS = 2  # at most this many echo throws inside a blend (ends of A's vocal phrases, breakdown exits)
_WASH_SIZE = 0.95  # reverb size of a reverb wash

# B's fader-in per loop-roll length, bars
_FADE_IN = {4: 1, 8: 2, 16: 4, 32: 8}

_Grid = transitions._Grid


# --- shared math ---------------------------------------------------------------------------------------------------


def _finite(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _num(x: object, default: float = 0.0) -> float:
    return float(x) if _finite(x) else default


def lane_value(keyframes: list[dict], beat: float | np.ndarray) -> float | np.ndarray:
    """Value of a keyframed lane at master beat(s). Constant before the first and after the last keyframe; between
    keyframe i and i+1 the shape is keyframe i's curve: linear, ease (smoothstep) or hold (value i until i+1).
    Two keyframes on one beat are an instant step (the later one wins from that beat on)."""
    if not keyframes:
        raise ValueError("lane has no keyframes")
    kf = sorted(keyframes, key=lambda k: float(k["beat"]))
    beats = np.array([float(k["beat"]) for k in kf])
    vals = np.array([float(k["value"]) for k in kf])
    curves = [k.get("curve", "linear") for k in kf]
    x = np.asarray(beat, dtype=float)
    i = np.clip(np.searchsorted(beats, x, side="right") - 1, 0, len(kf) - 1)
    j = np.minimum(i + 1, len(kf) - 1)
    span = beats[j] - beats[i]
    t = np.where(span > 0, (x - beats[i]) / np.where(span > 0, span, 1.0), 0.0)
    t = np.clip(t, 0.0, 1.0)
    code = np.array([0 if c == "hold" else 2 if c == "ease" else 1 for c in curves])[i]
    shape = np.where(code == 0, 0.0, np.where(code == 2, t * t * (3 - 2 * t), t))
    out = vals[i] + (vals[j] - vals[i]) * shape
    out = np.where(x < beats[0], vals[0], out)
    return float(out) if np.ndim(out) == 0 else out


def _deck_beat(deck: dict, beat: float | np.ndarray) -> np.ndarray:
    """Master beat(s) -> the deck's grid position, in master beats from its beat0_s.

    Loops use SLIP semantics (CDJ slip mode): inside [start_beat, end_beat) the deck plays
    source_beat + ((beat - start_beat) mod length_beats); outside every loop it is exactly where it would be had it
    never looped (the plain grid mapping), so on release it jumps to where it "would have been"."""
    x = np.asarray(beat, dtype=float)
    u = x.copy()
    for lp in deck.get("loops") or []:
        s, e = float(lp["start_beat"]), float(lp["end_beat"])
        src, ln = float(lp["source_beat"]), float(lp["length_beats"])
        if ln > 0 and e > s:
            u = np.where((x >= s) & (x < e), src + np.mod(x - s, ln), u)
    return u


def deck_time(deck: dict, beat: float | np.ndarray) -> float | np.ndarray:
    """File time (s) of a deck at master beat(s): beat0_s + position * 60 / native_bpm, where the position is the
    master beat itself, or source_beat + ((beat - start_beat) mod length_beats) inside a loop (slip semantics: a
    released loop leaves the deck where it would have been without it). Times before 0 or past the file's end are
    silence."""
    u = _deck_beat(deck, beat)
    out = float(deck["beat0_s"]) + u * 60.0 / float(deck["native_bpm"])
    return float(out) if np.ndim(out) == 0 else out


def master_seconds(tempo: list[dict], beats: float | np.ndarray, origin: float = 0.0) -> float | np.ndarray:
    """Seconds from master beat `origin` to `beats` under the tempo lane (BPM keyframes), numerically integrated."""
    x = np.asarray(beats, dtype=float)
    lo, hi = min(origin, float(np.min(x))), max(origin, float(np.max(x)))
    n = max(2, int(math.ceil((hi - lo) * 32)) + 1)
    grid = np.linspace(lo, hi, n)
    spb = 60.0 / np.maximum(np.asarray(lane_value(tempo, grid), dtype=float), 1e-6)
    cum = np.concatenate([[0.0], np.cumsum((spb[1:] + spb[:-1]) * 0.5 * np.diff(grid))])
    out = np.interp(x, grid, cum) - np.interp(origin, grid, cum)
    return float(out) if np.ndim(out) == 0 else out


# --- validation ----------------------------------------------------------------------------------------------------


def _req_num(d: dict, key: str, what: str) -> float:
    v = d.get(key)
    if not _finite(v) or abs(v) > _MAX_ABS_BEAT * 10:
        raise ValueError(f"{what}.{key} must be a finite number")
    return float(v)


def _keyframes(raw: object, lo: float, hi: float, what: str) -> list[dict] | None:
    """Clamped, beat-sorted keyframes; None for a missing/empty lane; ValueError on garbage."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError(f"{what} must be a list of keyframes")
    if len(raw) > _MAX_KEYFRAMES:
        raise ValueError(f"{what} has too many keyframes")
    out = []
    for k in raw:
        if not isinstance(k, dict):
            raise ValueError(f"{what}: keyframe must be an object")
        beat, value = k.get("beat"), k.get("value")
        if not _finite(beat) or abs(beat) > _MAX_ABS_BEAT or not _finite(value):
            raise ValueError(f"{what}: keyframe beat/value must be finite numbers")
        curve = k.get("curve") if k.get("curve") in CURVES else "linear"
        out.append({"beat": float(beat), "value": min(hi, max(lo, float(value))), "curve": curve})
    out.sort(key=lambda k: k["beat"])
    return out or None


def _loops(raw: object, what: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > _MAX_LOOPS:
        raise ValueError(f"{what}.loops must be a list of at most {_MAX_LOOPS} loops")
    out = []
    for lp in raw:
        if not isinstance(lp, dict):
            raise ValueError(f"{what}.loops: loop must be an object")
        s, e = _req_num(lp, "start_beat", what), _req_num(lp, "end_beat", what)
        src, ln = _req_num(lp, "source_beat", what), _req_num(lp, "length_beats", what)
        if ln < _MIN_LOOP_BEATS or ln > 256:
            raise ValueError(f"{what}.loops: length_beats must be between 1/16 and 256")
        if e <= s:
            raise ValueError(f"{what}.loops: end_beat must be after start_beat")
        out.append({"start_beat": s, "end_beat": e, "source_beat": src, "length_beats": ln})
    out.sort(key=lambda lp: lp["start_beat"])
    for p, q in zip(out, out[1:]):
        if q["start_beat"] < p["end_beat"] - 1e-9:
            raise ValueError(f"{what}.loops overlap")
    return out


def _tap(raw: object) -> str:
    return raw if raw in SEND_TAPS else SEND_TAPS[0]


def _deck(raw: object, what: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"plan.{what} must be a deck object")
    tid = raw.get("track_id")
    if not isinstance(tid, str) or not tid:
        raise ValueError(f"{what}.track_id must be a non-empty string")
    bpm = _req_num(raw, "native_bpm", what)
    if not 20.0 <= bpm <= 400.0:
        raise ValueError(f"{what}.native_bpm out of range")
    p0, p1 = _req_num(raw, "play_from_beat", what), _req_num(raw, "play_to_beat", what)
    if p0 >= p1:
        raise ValueError(f"{what}: play_from_beat must be before play_to_beat")
    lanes_raw = raw.get("lanes") if isinstance(raw.get("lanes"), dict) else {}
    lanes = {}
    for lane in LANES:
        lo, hi = LANE_RANGES[lane]
        kf = _keyframes(lanes_raw.get(lane), lo, hi, f"{what}.lanes.{lane}")
        lanes[lane] = kf or [{"beat": 0.0, "value": LANE_DEFAULTS[lane], "curve": "hold"}]
    echo = _num(raw.get("echo_beats"), 0.75)
    return {
        "track_id": tid, "beat0_s": _req_num(raw, "beat0_s", what), "native_bpm": bpm,
        "play_from_beat": p0, "play_to_beat": p1,
        "echo_beats": min(ECHO_BEATS_RANGE[1], max(ECHO_BEATS_RANGE[0], echo)),
        "echo_tap": _tap(raw.get("echo_tap")), "reverb_tap": _tap(raw.get("reverb_tap")),
        "reverb_size": min(1.0, max(0.0, _num(raw.get("reverb_size"), REVERB_SIZE))),
        "loops": _loops(raw.get("loops"), what), "lanes": lanes,
    }  # fmt: skip


def validate(plan: dict) -> dict:
    """Normalised copy of a MixPlan: clamped lane values, sorted keyframes, every lane present, sane loops, window
    clamped into the decks' play range. ValueError when it can't be a plan (missing decks, garbage numbers...)."""
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    p = copy.deepcopy(plan)
    for key in ("from_id", "to_id"):
        if not isinstance(p.get(key), str) or not p[key]:
            raise ValueError(f"plan.{key} must be a non-empty string")
    if not isinstance(p.get("folder", ""), str):
        raise ValueError("plan.folder must be a string")
    if p.get("technique") not in TECHNIQUES:
        raise ValueError(f"unknown technique {p.get('technique')!r}")
    bars = p.get("bars")
    if not _finite(bars):
        raise ValueError("plan.bars must be 4, 8, 16 or 32")
    a, b = _deck(p.get("a"), "a"), _deck(p.get("b"), "b")
    tempo = _keyframes(p.get("tempo"), *TEMPO_RANGE, "tempo") or [
        {"beat": 0.0, "value": min(TEMPO_RANGE[1], max(TEMPO_RANGE[0], a["native_bpm"])), "curve": "hold"}
    ]
    lo, hi = min(a["play_from_beat"], b["play_from_beat"]), max(a["play_to_beat"], b["play_to_beat"])
    w = p.get("window") if isinstance(p.get("window"), dict) else {}
    ws = max(lo, _num(w.get("start_beat"), lo))
    we = min(hi, _num(w.get("end_beat"), hi))
    if we - ws < 1.0:
        raise ValueError("window is empty or outside the decks' play range")
    if we - ws > _MAX_WINDOW_BEATS:
        raise ValueError(f"window longer than {_MAX_WINDOW_BEATS} beats")
    markers = []
    for m in p.get("markers") if isinstance(p.get("markers"), list) else []:
        if isinstance(m, dict) and _finite(m.get("beat")) and isinstance(m.get("label"), str) and m["label"]:
            markers.append({"beat": float(m["beat"]), "label": m["label"][:40]})
    markers.sort(key=lambda m: m["beat"])
    notes = [n for n in (p.get("notes") if isinstance(p.get("notes"), list) else []) if isinstance(n, str)][:40]
    return {
        "version": PLAN_VERSION, "folder": p.get("folder", ""), "from_id": p["from_id"], "to_id": p["to_id"],
        "technique": p["technique"], "bars": min(BARS, key=lambda n: abs(n - bars)),
        "key_lock": bool(p.get("key_lock", True)), "window": {"start_beat": ws, "end_beat": we},
        "tempo": tempo, "a": a, "b": b, "markers": markers, "notes": notes, "edited": bool(p.get("edited", False)),
    }  # fmt: skip


# --- planning: inputs ----------------------------------------------------------------------------------------------


@dataclass
class _Place:
    a0: int  # A bar sounding at master bar 0
    b0: int  # B bar sounding at master bar 0
    overlap: int  # bars both decks play (0 = cut on the 1 at master beat 0)
    swap: int | None  # 0-based overlap bar where B's low end replaces A's; None = B's lows come in as A leaves
    b_loop: transitions.Loop | None = None  # B loops a phrase of its short intro from its cue (transitions._b_loop)


@dataclass
class _Mix:
    a: Track
    b: Track
    ga: _Grid
    gb: _Grid
    real_a: bool
    real_b: bool
    a_bpm: float  # A's grid tempo
    b_bpm: float  # B's tempo on the master grid (analysis BPM x factor)
    b_raw_bpm: float
    factor: float
    move: str
    tr: dict | None  # transitions.suggest output (both analysed only)
    a_dur: float
    b_dur: float
    pref: str = "normal"  # mix length preference (settings.mix_length)

    @property
    def length(self) -> int:
        """Preferred blend length in bars: 8 / 16 / 32 for short / normal / long."""
        return transitions.MIX_LENGTHS.get(self.pref, 16)


def _fake_grid(bpm: float, duration_s: float) -> _Grid:
    """Bar grid from the BPM alone (no analysis): bar 0 at 0 s, 8-bar phrases, no known structure."""
    bar_s = 240.0 / bpm
    n = max(24, int(duration_s / bar_s)) if duration_s > 0 else 128
    pts = phrase_grid([], 0, n)
    return _Grid(
        fd=0.0, bar_s=bar_s, n=n, vocal=(0.0,) * n, low=(0.6,) * n, energy=(0.7,) * n, perc=(False,) * n,
        intro=(0, 0), outro=(n, n), intro_drums=False, intro_vocals=False, outro_drums=False, outro_vocals=False,
        phrases=tuple(p for p, _ in pts), levels=tuple(lv for _, lv in pts), drops=(), end=n, off=0,
    )  # fmt: skip


def _mix(
    a: Track, a_an: dict | None, b: Track, b_an: dict | None, transition: dict | None, pref: str = "normal"
) -> _Mix:
    a_an = a_an if isinstance(a_an, dict) else None
    b_an = b_an if isinstance(b_an, dict) else None
    a_bpm, b_bpm = transitions._tempo(a, a_an)[0], transitions._tempo(b, b_an)[0]
    a_bpm = a_bpm if a_bpm > 0 else b_bpm if b_bpm > 0 else _DEFAULT_BPM
    b_bpm = b_bpm if b_bpm > 0 else a_bpm
    p = mixing._pitch(a_bpm, b_bpm)
    factor = p[1] if p else 1.0
    ga, gb = transitions._grid(a_an), transitions._grid(b_an, factor)
    a_dur = _num((a_an or {}).get("duration_s"), a.duration_s or 0.0)
    b_dur = _num((b_an or {}).get("duration_s"), b.duration_s or 0.0)
    tr = None
    if ga is not None and gb is not None:
        ok = isinstance(transition, dict) and transition.get("ready") and transition.get("from_id") == a.id \
            and transition.get("to_id") == b.id and _finite(transition.get("out_start_s")) \
            and _finite(transition.get("in_start_s"))
        tr = transition if ok else transitions.suggest(a, a_an, b, b_an, mix_length=pref)
    return _Mix(
        a=a, b=b, ga=ga or _fake_grid(a_bpm, a_dur), gb=gb or _fake_grid(b_bpm * factor, b_dur),
        real_a=ga is not None, real_b=gb is not None, a_bpm=a_bpm, b_bpm=b_bpm * factor, b_raw_bpm=b_bpm,
        factor=factor, move=mixing._camelot_move(a.camelot, b.camelot), tr=tr, a_dur=a_dur, b_dur=b_dur, pref=pref,
    )  # fmt: skip


def _mean(arr: tuple[float, ...], lo: int, hi: int) -> float:
    vals = arr[max(0, lo) : max(0, hi)]
    return sum(vals) / len(vals) if vals else 0.0


def _swap(ga: _Grid, gb: _Grid, a0: int, b0: int, length: int, b_loop: transitions.Loop | None = None) -> int | None:
    if length <= 0:
        return None
    a_loop = transitions._a_loop(ga, a0, length)
    return transitions._swap_offset(ga, gb, transitions._Plan("", a0, b0, length, 0.0, [], 0, a_loop, b_loop))


# --- planning: placement -------------------------------------------------------------------------------------------


def _engine(m: _Mix) -> tuple[str, _Place]:
    """The suggestion engine's technique and bars; without both analyses a plain bass swap of the preferred length
    (16 bars by default) near A's end."""
    ga, gb, tr = m.ga, m.gb, m.tr
    if tr is not None and tr.get("ready") and tr.get("technique") in transitions.TECHNIQUES:
        a0 = max(0, min(ga.n, round((tr["out_start_s"] - ga.fd) / ga.bar_s)))
        b0 = max(0, min(gb.n - 1, round((tr["in_start_s"] - gb.fd) / gb.bar_s)))
        length = int(_num(tr.get("overlap_bars")))
        sw = tr.get("swap_bar")
        swap = sw - 1 if isinstance(sw, int) and 0 < sw <= length else None
        b_loop = transitions._b_loop(gb, b0, length) if m.real_b and _num(tr.get("b_loop_bars")) > 0 else None
        return tr["technique"], _Place(a0, b0, length, swap, b_loop)
    return "bass swap", _fallback_place(m, min(m.length, 16))


def _fallback_place(m: _Mix, length: int) -> _Place:
    """B in on A's phrase so the blend ends a phrase before A's end (or at its outro end when A is analysed)."""
    ga, gb = m.ga, m.gb
    end = ga.end if m.real_a else max(length, ga.end - _PHRASE)
    fits = [p for p in ga.phrases if p + length <= end]
    strong = [p for p in fits if ga.level(p) >= 16 and p >= fits[-1] - _PHRASE]  # a 16-bar phrase start if close
    a0 = (strong or fits)[-1] if fits else max(0, end - length)
    b0 = transitions._first_cue(gb) if m.real_b else 0
    return _Place(a0, b0, length, _swap(ga, gb, a0, b0, length))


def _best_of_length(m: _Mix, length: int) -> _Place | None:
    """Best phrase-aligned overlap of exactly `length` (8/16/32) bars, scored by transitions' own window scorer."""
    ga, gb = m.ga, m.gb
    best = None
    for w in transitions._windows(ga, gb, length, m.move == "clash", m.pref):
        if best is None or w.fit > best.fit + 1e-9:
            best = w
    if best is None:
        return None
    return _Place(best.a0, best.b0, length, _swap(ga, gb, best.a0, best.b0, length, best.b_loop), best.b_loop)


def _blend_place(m: _Mix, length: int, eng: tuple[str, _Place]) -> _Place:
    e_tech, e = eng
    if length == 4:
        base = _blend_place(m, 8, eng)
        return _Place(base.a0 + 4, base.b0 + 4, 4, _swap(m.ga, m.gb, base.a0 + 4, base.b0 + 4, 4))
    if e.overlap == length and e_tech not in _CUTS:
        return e
    if m.real_a and m.real_b:
        best = _best_of_length(m, length)
        if best is not None:
            return best
    return _fallback_place(m, length)


def _cut_place(m: _Mix, technique: str, eng: tuple[str, _Place]) -> _Place:
    ga, gb = m.ga, m.gb
    e_tech, e = eng
    if e_tech == technique and e.overlap == 0:
        return e
    if technique == "drop swap":
        a_drops = [d for d in ga.drops if ga.end * 0.4 <= d <= ga.end - _PHRASE]
        b_drops = [d for d in gb.drops if d > gb.intro[0]]
        if a_drops and b_drops:
            return _Place(a_drops[-1], b_drops[0], 0, None)
    if technique == "slam":
        # the end of A's build (else where its outro starts, on its phrase) on the 1 of B's first drop / main section
        a0 = _a_build_end(m)
        if a0 is None:
            o0 = ga.outro[0]
            cands = [p for p in ga.phrases if ga.end // 2 <= p <= ga.end]
            a0 = min(cands, key=lambda p: (abs(p - o0), -p)) if cands and o0 < ga.end else transitions._cut_bar(ga)
        b_drops = [d for d in gb.drops if d > gb.intro[0]]
        b0 = b_drops[0] if b_drops else gb.intro[1] if 0 < gb.intro[1] < gb.n else transitions._first_cue(gb)
        return _Place(a0, b0, 0, None)
    if e.overlap == 0 and e_tech != "drop swap":
        return _Place(e.a0, e.b0, 0, None)
    end = ga.end if m.real_a else max(_PHRASE, ga.end - _PHRASE)
    a0 = transitions._cut_bar(ga) if m.real_a else max(p for p in ga.phrases if p <= end)
    return _Place(a0, transitions._first_cue(gb) if m.real_b else 0, 0, None)


def _loop_block(m: _Mix, a0: int, length: int) -> tuple[int, int] | None:
    """(q, size): when A runs out of music before the roll, loop A's bars [q - size, q) (q on A's 4-bar grid from
    a0, size 8 or 4, least vocal); None when A has enough music for the whole blend."""
    ga = m.ga
    if a0 + length - 2 < ga.end:  # the roll's bar is still A's music
        return None
    q = a0 + max(0, (ga.end - a0) // 4 * 4)
    if q < 8:
        return None
    eight, four = _mean(ga.vocal, q - 8, q), _mean(ga.vocal, q - 4, q)
    return (q, 8) if eight < _LOOP_VOCAL_MAX or eight <= four else (q, 4)


def _clear_a_drop(m: _Mix, place: _Place) -> tuple[_Place, str | None]:
    """Don't bring B in over A's breakdown with A's last drop still to come: start the blend a phrase into it."""
    inside = [d for d in m.ga.drops if place.a0 < d < place.a0 + place.overlap]
    if not (m.real_a and inside):
        return place, None
    cands = [p for p in m.ga.phrases if p >= inside[-1] + _PHRASE and p + place.overlap <= m.ga.end]
    if not cands:
        return place, None
    return _Place(cands[0], place.b0, place.overlap, place.swap, place.b_loop), \
        f"B comes in a phrase into A's last drop (A bar {cands[0] + 1}) rather than over the breakdown before it."


def _land_on_drop(m: _Mix, place: _Place, fixed_len: bool) -> tuple[_Place, str | None]:
    """When B goes <intro> <chill> <drop> and the blend would end in the chill, end it on B's drop: lengthen it to
    16/32 bars over A's outro when A has the room (and the length isn't fixed), else cue B later so the overlap runs
    through the chill; either way A rides to the drop and the lows change hands on its 1."""
    if not (m.real_a and m.real_b) or not place.overlap or place.b_loop is not None:
        return place, None
    gb = m.gb
    end_b = place.b0 + place.overlap
    if any(place.b0 < d <= end_b for d in gb.drops):
        return place, None
    later = [d for d in gb.drops if d > end_b]
    if not later or later[0] - end_b > 48:
        return place, None
    d = later[0]
    if not (_mean(gb.energy, end_b, d) < 0.8 * _mean(gb.energy, d, d + _PHRASE) or _mean(gb.low, end_b, d) < 0.5):
        return place, None
    need = d - place.b0
    if not fixed_len and need in (16, 32) and place.a0 + need <= m.ga.end:
        return _Place(place.a0, place.b0, need, None), \
            f"Blend stretched to {need} bars so A leaves right on B's drop (B {_mmss(gb, d)}) instead of B's quiet part."
    b0 = d - place.overlap
    return _Place(place.a0, b0, place.overlap, None), \
        f"B is cued {b0 - place.b0} bars later (B {_mmss(gb, b0)}) so its quiet part runs under A and A leaves " \
        f"right on B's drop (B {_mmss(gb, d)})."


def _hits(g: _Grid, d: int) -> bool:
    """The drop at bar d hits: full power, clearly louder than the 4 bars of build before it."""
    hit = _mean(g.energy, d, d + 4)
    return hit >= transitions._LOUD and hit >= _SLAM_JUMP * _mean(g.energy, d - 4, d)


def _b_slam_drop(m: _Mix) -> int | None:
    """B's first drop when it hits right after a short intro (nothing to blend over: slam into it)."""
    gb = m.gb
    i0, i1 = gb.intro
    if not m.real_b or i1 - i0 > _SLAM_INTRO:
        return None
    d = next((d for d in gb.drops if d > i0), None)
    return d if d is not None and d - i1 <= _SLAM_LEAD and _hits(gb, d) else None


def _a_build_end(m: _Mix) -> int | None:
    """Bar where A's last build ends (the 1 of the drop it leads into) when that is near A's end: a slam cuts there,
    A's build lined up with B's drop."""
    ga = m.ga
    if not m.real_a:
        return None
    return next((d for d in reversed(ga.drops) if ga.end - _BUILD_BARS <= d <= ga.end - 4 and _hits(ga, d)), None)


def _auto_technique(m: _Mix, e_tech: str, e: _Place) -> str:
    """Planner technique for the engine's pick: a slam when B's first drop hits right after a short intro, or when
    A has a build to cut out of and the engine would cut anyway (or the tempo gap is too wide to blend); loop roll
    when A runs out under a long clean intro of B, a filter sweep over awkward keys or a vocal outro, long blend for
    long in-key blends, else the engine's own."""
    gap = abs(m.b_bpm / m.a_bpm - 1)
    would_cut = gap > _MAX_BLEND_GAP or (e_tech in ("quick cut", "echo out") and not e.overlap)
    if m.real_a and m.real_b and (_b_slam_drop(m) is not None or (would_cut and _a_build_end(m) is not None)):
        return "slam"
    if e.overlap and gap > _MAX_BLEND_GAP:
        return "echo out"
    if e.overlap == 0 or not (m.real_a and m.real_b):
        return e_tech
    ga, gb = m.ga, m.gb
    good_key = m.move in _GOOD_KEYS
    if e.overlap <= 8 and ga.end - (e.a0 + e.overlap) <= 1 and gb.intro[1] - e.b0 >= 16 and good_key \
            and gap <= _LOOP_ROLL_GAP and _mean(gb.vocal, e.b0, e.b0 + 16) < _LOOP_VOCAL_MAX:
        blk = _loop_block(m, e.a0, 16)
        if blk is not None and _mean(ga.vocal, blk[0] - blk[1], blk[0]) < _LOOP_VOCAL_MAX:
            return "loop roll"
    if e_tech == "echo out":
        return e_tech
    a_vocal = _mean(ga.vocal, e.a0, e.a0 + e.overlap)
    b_vocal = _mean(gb.vocal, e.b0, e.b0 + e.overlap)
    tonal_a = not all(ga.perc[e.a0 : e.a0 + e.overlap])
    if (m.move in _SWEEP_KEYS and tonal_a) or (a_vocal >= 0.5 and b_vocal < 0.3):
        return "filter sweep"
    if e.overlap >= 16 and good_key and gap <= _SMALL_GAP:
        return "long blend"
    return e_tech


def _default_bars(m: _Mix, technique: str, e: _Place) -> int:
    if technique == "loop roll":
        return 16 if m.gb.intro[1] - e.b0 >= 16 or not m.real_b else 8
    if technique == "long blend":
        return e.overlap if e.overlap in (16, 32) else max(16, m.length)
    return e.overlap if e.overlap in BARS else m.length


# --- planning: tempo -----------------------------------------------------------------------------------------------


def _ramp_bars(frac: float) -> int:
    bars = _PHRASE * math.ceil(abs(frac) / _RAMP_PER_8 - 1e-9)
    return int(min(_RAMP_MAX, max(_RAMP_MIN, bars)))


@dataclass
class _Tempo:
    keyframes: list[tuple[float, float, str]]
    pre_start: float | None  # master beat where A starts ramping before B comes in
    post_end: float | None  # master beat where the drift to B's tempo ends
    meet: float  # tempo while both decks play
    note: str


def _tempo(m: _Mix, end: float, cut: bool) -> _Tempo:
    a, b = m.a_bpm, m.b_bpm
    gap = b / a - 1
    half = ""
    if m.factor != 1:
        half = f"B is {'half' if m.factor == 2 else 'double'}-time ({m.b_raw_bpm:.0f} BPM), mixed at {b:.1f}. "
    if abs(gap) < 0.0005:
        return _Tempo([(0.0, a, "hold")], None, None, a, f"{half}Same tempo ({a:.1f} BPM), no pitch moves.")
    if cut and abs(gap) > _CUT_JUMP_GAP:
        return _Tempo([(-1.0, a, "hold"), (0.0, b, "hold")], None, None, a,
                      f"{half}A plays at its own {a:.1f} BPM and B starts at its own {b:.1f} BPM after the cut "
                      f"({gap * 100:+.1f}%) — the cut hides the tempo change.")
    if abs(gap) <= _SMALL_GAP:
        post = _ramp_bars(gap)
        what = "the blend" if not cut else "the cut"
        return _Tempo([(0.0, a, "hold"), (end, a, "ease"), (end + 4 * post, b, "hold")], None, end + 4 * post, a,
                      f"{half}B is pitched {-gap / (1 + gap) * 100:+.1f}% to A's {a:.1f} BPM through {what} (key "
                      f"lock on), then the tempo drifts to B's own {b:.1f} BPM over {post} bars once A is out.")
    meet = a * math.sqrt(b / a)
    pre, post = _ramp_bars(meet / a - 1), _ramp_bars(b / meet - 1)
    kf = [(-4.0 * pre, a, "ease"), (0.0, meet, "hold"), (end, meet, "ease"), (end + 4 * post, b, "hold")]
    loud = " Big tempo change — these ramps will be audible; an echo out hides it better." \
        if abs(gap) > _MAX_BLEND_GAP else ""
    return _Tempo(kf, -4.0 * pre, end + 4 * post, meet,
                  f"{half}Tempo gap {gap * 100:+.1f}%: A drifts from {a:.1f} to {meet:.1f} BPM over the {pre} bars "
                  f"before B comes in, both play at {meet:.1f} through the blend (key lock on), then the tempo "
                  f"drifts on to B's own {b:.1f} BPM over {post} bars.{loud}")


# --- planning: lanes -----------------------------------------------------------------------------------------------


def _lanes() -> dict[str, list[tuple[float, float, str]]]:
    return {lane: [] for lane in LANES}


def _clash_bars(m: _Mix, a_deck: dict, b_deck: dict, place: _Place) -> list[int]:
    """Overlap bars (0-based) with vocals on both decks (each deck's bar read through its loops)."""
    if not place.overlap:
        return []
    k = np.arange(place.overlap)
    a_bars = place.a0 + np.floor(_deck_beat(a_deck, 4.0 * k + 0.5) / 4).astype(int)
    b_cue = place.b0 - (place.b_loop.bars if place.b_loop else 0)  # B's bar at master bar 0 without its loop
    b_bars = b_cue + np.floor(_deck_beat(b_deck, 4.0 * k + 0.5) / 4).astype(int)
    out = []
    for i, ab, bb in zip(k, a_bars, b_bars):
        va = m.ga.vocal[ab] if 0 <= ab < m.ga.n else 0.0
        vb = m.gb.vocal[bb] if 0 <= bb < m.gb.n else 0.0
        if va >= transitions._VOCAL_ON and vb >= transitions._VOCAL_ON:
            out.append(int(i))
    return out


def _tail(at: float, peak: float) -> list[tuple[float, float, str]]:
    """A send held at `peak` from the cut on `at` for a beat, then off over the next: A's fader is down by then but
    it still plays _SEND_TAIL beats into the send, and the tail rings on."""
    return [(at, peak, "hold"), (at + 1, peak, "linear"), (at + _SEND_TAIL, 0.0, "hold")]


def _throw(A: dict, at: float, send: float = 0.9, reverb: float = 0.6) -> dict:
    """Echo throw into the cut on the 1 at `at`: A's echo send ramps up over its last 2 beats and stays open a beat
    past the cut (pre-EQ, feedback 0.7: the repeats ring 2-4 bars over B), with a reverb wash over its last bar."""
    A["echo_send"] = [(at - 2, 0.0, "linear"), *_tail(at, send)]
    A["reverb_send"] = [(at - 4, 0.0, "linear"), *_tail(at, reverb)]
    return {"echo_beats": 0.75, "feedback": 0.7, "throw": at - 2}


def _phrases(length: int, swap: int | None) -> tuple[float, float, float]:
    """(fader-in end, in-phrase end, out-phrase start) of a blend, in master beats. The in and out phrases are 8 bars
    (the halves of an 8- or 4-bar blend); the bass swap, on a phrase boundary, closes the in-phrase early or holds
    the out-phrase back. B's fader comes up over a quarter of the blend: 8 bars of a 32, 4 of a 16."""
    E = 4.0 * length
    ph = 32.0 if length >= 16 else E / 2
    S = 4.0 * swap if swap is not None else None
    i_end = ph if S is None else min(ph, S)
    o_start = E - ph if S is None else max(E - ph, S)
    return min(i_end, max(4.0, E / 4)), i_end, o_start


def _mid_throws(m: _Mix, place: _Place) -> list[float]:
    """Master beats (on a 1) inside a blend where A gets a short echo throw: right after one of A's vocal phrases
    ends (2+ sung bars, then a bar without vocals) and on the 1 of a drop of either deck (a breakdown exit). At least
    2 bars after B comes in and 2 bars before A's own exit throw, 2 bars apart, at most _MID_THROWS (drops first)."""
    L = place.overlap
    if not (L and m.real_a):
        return []
    ga, gb = m.ga, m.gb
    on = transitions._VOCAL_ON

    def sung(bar: int) -> bool:
        return 0 <= bar < ga.n and ga.vocal[bar] >= on

    drops = {d - place.a0 for d in ga.drops} | ({d - place.b0 for d in gb.drops} if m.real_b else set())
    phrase_ends = {k for k in range(L) if sung(place.a0 + k - 1) and sung(place.a0 + k - 2) and not sung(place.a0 + k)}
    out: list[float] = []
    for k in sorted(k for k in drops if 2 <= k <= L - 2) + sorted(k for k in phrase_ends if 2 <= k <= L - 2):
        if len(out) < _MID_THROWS and all(abs(4.0 * k - t) >= 8.0 for t in out):
            out.append(4.0 * k)
    return sorted(out)


def _throw_points(at: float, send: float = 0.8) -> list[tuple[float, float, str]]:
    """A short echo throw into the 1 at `at`: the send jumps up on the last beat before it and closes half a beat
    after, so the echo catches the end of the phrase and its repeats ring into the next."""
    return [(at - 1.0, 0.0, "linear"), (at - 0.75, send, "hold"), (at, send, "linear"), (at + 0.5, 0.0, "hold")]


def _blend_lanes(tech: str, length: int, swap: int | None, A: dict, B: dict, throws: list[float]) -> dict:
    """Blend automation (see _blend_body) plus short echo throws on A at the master beats in `throws` (ends of its
    vocal phrases, breakdown exits)."""
    info = _blend_body(tech, length, swap, A, B)
    A["echo_send"] = [p for t in throws for p in _throw_points(t)] + A["echo_send"]
    info["throws"] = [t - 1.0 for t in throws]
    return info


def _blend_body(tech: str, length: int, swap: int | None, A: dict, B: dict) -> dict:
    """Blend automation in phrases (same shape for 8/16/32 bars, see _phrases):
    - in: B comes in with its lows killed behind a gentle filter, its fader up over 4-8 bars, its highs and filter
      opening over the phrase while its mids stay 4 dB down;
    - ride: both decks play together; the bass swaps on the 1 of the swap bar with a 1-beat crossfade, B's mids
      come fully up over the bar after it;
    - out: the last phrase takes A out, highs first, then mids, then the fader, under a slowly rising high-pass, its
      reverb swelling over its last 2 bars and echo over its last, both fed a beat past the cut.
    Without a swap (B brings no bass, or A rides until B's drop) A's bass carries to the end and the lows change
    hands on the 1 as A leaves. A filter sweep runs A's high-pass over the whole blend; an echo out keeps A's fader
    up and cuts it on the 1 under an echo throw."""
    E = 4.0 * length
    S = 4.0 * swap if swap is not None else None
    fade_in, i_end, o_start = _phrases(length, swap)
    out = E - o_start
    info: dict = {"echo_beats": 0.75, "feedback": 0.6, "throw": None}
    mids_full = S if S is not None else o_start
    B["volume"] = [(0.0, 0.0, "ease"), (fade_in, 1.0, "hold")]
    B["eq_high"] = [(0.0, -12.0, "ease"), (i_end, 0.0, "hold")]
    B["eq_mid"] = [(0.0, -15.0, "ease"), (i_end, -4.0, "hold"), (mids_full, -4.0, "ease"),
                   (min(E, mids_full + 4), 0.0, "hold")]
    B["filter"] = [(0.0, -0.6 if tech == "filter sweep" else 0.4 if tech == "long blend" else 0.3, "ease"),
                   (i_end, 0.0, "hold")]
    if S is not None:
        B["eq_low"] = [(0.0, _KILL, "hold"), (S, _KILL, "linear"), (S + 1, 0.0, "hold")]
        A["eq_low"] = [(0.0, 0.0, "hold"), (S, 0.0, "linear"), (S + 1, _KILL, "hold")]
    else:
        B["eq_low"] = [(0.0, _KILL, "hold"), (E, 0.0, "hold")]
    sweep = 0.85 if tech == "filter sweep" else 0.35
    A["filter"] = [(0.0 if tech == "filter sweep" else o_start, 0.0, "ease"), (E, sweep, "hold")]
    if tech == "echo out":  # A's fader stays up; cut on the 1 under a long echo throw
        A["eq_mid"] = [(o_start, 0.0, "ease"), (E, -6.0, "hold")]
        A["eq_high"] = [(o_start, 0.0, "ease"), (E, -6.0, "hold")]
        A["volume"] = [(E - 4, 1.0, "hold"), (E, 0.0, "hold")]
        info.update(_throw(A, E))
        return info
    if S is None:  # A rides, bass and full energy, to the 1 where B's lows land (no dip before B's drop)
        A["eq_mid"] = [(o_start, 0.0, "ease"), (E, -6.0, "hold")]
        A["eq_high"] = [(o_start, 0.0, "ease"), (E, -6.0, "hold")]
        A["filter"] = [(E - 8, 0.0, "ease"), (E, 0.12, "hold")]
        A["volume"] = [(E - 4, 1.0, "hold"), (E, 0.0, "hold")]
    else:  # highs over the first half of the out-phrase, mids over its middle, the fader over its second half
        A["eq_high"] = [(o_start, 0.0, "ease"), (o_start + out / 2, -12.0, "hold")]
        A["eq_mid"] = [(o_start + out / 4, 0.0, "ease"), (o_start + 3 * out / 4, -10.0, "hold")]
        A["volume"] = [(o_start + out / 2, 1.0, "ease"), (E, 0.0, "hold")]
    A["echo_send"] = [(E - 4, 0.0, "linear"), *_tail(E, 0.6 if S is not None else 0.45)]
    A["reverb_send"] = [(E - 8, 0.0, "linear"), *_tail(E, 0.7 if tech == "filter sweep" else 0.55)]
    return info


def _duck(points: list[tuple[float, float, str]], x0: float, x1: float | None) -> list[tuple[float, float, str]]:
    """A mids lane ducked to -18 dB from master beat x0 (eased down over the 2 bars before) to x1, then eased back
    to its planned value over 2 bars; x1=None keeps it down to the end."""
    kf = [{"beat": b, "value": v, "curve": c} for b, v, c in points] or [{"beat": 0.0, "value": 0.0, "curve": "hold"}]

    def planned(x: float) -> float:
        return float(lane_value(kf, x))

    r0 = max(0.0, x0 - 8.0)
    pts = [p for p in points if p[0] < r0]
    pts += [(r0, _VOCAL_CUT, "hold")] if r0 >= x0 else [(r0, planned(r0), "ease"), (x0, _VOCAL_CUT, "hold")]
    if x1 is not None:
        back = x1 + 8.0
        curve = next((p[2] for p in reversed(points) if p[0] <= back), "hold")
        pts += [(x1, _VOCAL_CUT, "ease"), (back, planned(back), curve)]
        pts += [p for p in points if p[0] > back]
    return pts


def _duck_mids(A: dict, B: dict, clash: list[int], swap: int | None, length: int) -> tuple[list[int], list[int]]:
    """Vocals on both decks: duck the deck that isn't leading instead of shortening the blend. Before the swap (the
    middle without one) A leads, so B's mids go down through the clash and come back after it; from the swap on B
    leads and A, on its way out, stays ducked. Returns the (B, A) clash bars ducked."""
    pivot = swap if swap is not None else length // 2
    on_b, on_a = [k for k in clash if k < pivot], [k for k in clash if k >= pivot]
    if on_b:
        B["eq_mid"] = _duck(B["eq_mid"], 4.0 * on_b[0], 4.0 * (on_b[-1] + 1))
    if on_a:
        A["eq_mid"] = _duck(A["eq_mid"], 4.0 * on_a[0], None)
    return on_b, on_a


def _roll(at: float, src: float) -> list[dict]:
    """Loops of a roll into the 1 at master beat `at`: the bar from `at - 8` (deck beat `src`) rolls 1 bar -> 2
    beats -> 1 -> ½ over the last 2 bars, the ½ beat held 2 beats past the 1 for the echo to catch."""
    R = at - 8.0
    return [{"start_beat": s, "end_beat": e, "source_beat": src, "length_beats": ln}
            for s, e, ln in ((R, R + 4, 4.0), (R + 4, R + 6, 2.0), (R + 6, R + 7, 1.0), (R + 7, at + 2, 0.5))]


def _loop_roll(m: _Mix, place: _Place, A: dict, B: dict, a_deck: dict) -> dict:
    """A loops its last bars if it runs out, rolls 1 bar -> 2 -> 1 -> 1/2 beat over the last 2 bars, echo on the
    last half beat, cut on the 1 as B's bass drops in."""
    length = place.overlap
    E = 4.0 * length
    R = E - 8.0  # roll start
    loops = []
    blk = _loop_block(m, place.a0, length)
    src_roll = R
    if blk is not None:
        q, size = blk
        start, src, ln = 4.0 * (q - place.a0), 4.0 * (q - size - place.a0), 4.0 * size
        if R > start:
            loops.append({"start_beat": start, "end_beat": R, "source_beat": src, "length_beats": ln})
            src_roll = src + math.fmod(R - start, ln)
    loops += _roll(E, src_roll)
    a_deck["loops"] = loops
    fi = 4.0 * _FADE_IN[length]
    B["volume"] = [(0.0, 0.0, "ease"), (fi, 1.0, "hold")]
    B["eq_low"] = [(0.0, _KILL, "hold"), (E, 0.0, "hold")]
    B["eq_high"] = [(0.0, -12.0, "ease"), (E / 2, 0.0, "hold")]
    B["eq_mid"] = [(0.0, -15.0, "ease"), (E / 2, 0.0, "hold")]
    B["filter"] = [(0.0, 0.3, "ease"), (E / 2, 0.0, "hold")]
    A["eq_low"] = [(0.0, 0.0, "hold"), (R, _KILL, "hold")]
    A["filter"] = [(R, 0.0, "ease"), (E, 0.6, "hold")]
    A["volume"] = [(R, 1.0, "hold"), (E, 0.0, "hold")]
    info = _throw(A, E)
    info["loop"] = loops[0] if blk and len(loops) > 4 else None
    return info


def _cut_lanes(tech: str, bars: int, A: dict, into_drop: bool) -> dict:
    """Cuts on the 1 at master beat 0, B straight in at full. Into B's drop: a clean cut after a short filter riser
    and a 1-beat reverb swell on A; otherwise an echo throw on A (with a high-pass riser for echo outs). Slams have
    their own (_slam_lanes)."""
    info: dict = {"echo_beats": 0.75, "feedback": LANE_DEFAULTS["echo_feedback"], "throw": None}
    A["volume"] = [(-4.0, 1.0, "hold"), (0.0, 0.0, "hold")]
    if tech == "reverb wash":  # the reverb send swells over 4-8 bars under a rising high-pass, then the cut
        ramp = 4.0 * max(4, min(bars, 8))
        A["filter"] = [(-ramp, 0.0, "ease"), (0.0, 0.55, "hold")]
        A["reverb_send"] = [(-ramp, 0.0, "ease"), *_tail(0.0, 0.9)]
        info["reverb_size"] = _WASH_SIZE
        return info
    if into_drop:
        A["filter"] = [(-8.0, 0.0, "ease"), (0.0, 0.35, "hold")]
        A["reverb_send"] = [(-1.0, 0.0, "linear"), *_tail(0.0, 0.6)]
        return info
    if tech == "echo out":
        riser = 4.0 * max(2, min(bars, 8))
        A["filter"] = [(-riser, 0.0, "ease"), (0.0, 0.4, "hold")]
    info.update(_throw(A, 0.0))
    return info


def _slam_lanes(m: _Mix, place: _Place, bars: int, A: dict, B: dict, a_deck: dict, b_deck: dict) -> dict:
    """Slam on the 1 at master beat 0, B straight in at full. A rides a high-pass riser (a short one into B's drop)
    and an echo/reverb throw on the cut; when its last 2 bars aren't sung it rolls them with its lows out on the last
    bar. Into B's drop at about A's tempo, B's last build bar plays under A's last bar, lows killed and high-passed."""
    ga, gb = m.ga, m.gb
    into_drop = place.b0 in gb.drops
    A["volume"] = [(-4.0, 1.0, "hold"), (0.0, 0.0, "hold")]
    riser = 8.0 if into_drop else 4.0 * max(2, min(bars, 8))
    A["filter"] = [(-riser, 0.0, "ease"), (0.0, 0.35 if into_drop else 0.4, "hold")]
    info = _throw(A, 0.0, send=0.7 if into_drop else 0.9)
    info.update(roll=False, pre_roll=False)
    if m.real_a and place.a0 >= 2 and _mean(ga.vocal, place.a0 - 2, place.a0) < _LOOP_VOCAL_MAX:
        a_deck["loops"] = _roll(0.0, -8.0)
        A["eq_low"] = [(-8.0, 0.0, "hold"), (-4.0, _KILL, "hold")]
        info["roll"] = True
    # over a wider tempo gap B would be stretched to A's tempo for that bar and jump to its own on the 1: no pre-roll
    if into_drop and gb.fd + (place.b0 - 1) * gb.bar_s >= 0 and abs(m.b_bpm / m.a_bpm - 1) <= _CUT_JUMP_GAP:
        b_deck["play_from_beat"] = -4.0
        B["volume"] = [(-4.0, 0.0, "ease"), (-1.0, 0.6, "hold"), (0.0, 1.0, "hold")]
        B["eq_low"] = [(-4.0, _KILL, "hold"), (0.0, 0.0, "hold")]
        B["filter"] = [(-4.0, 0.5, "ease"), (-1.0, 0.25, "hold"), (0.0, 0.0, "hold")]
        info["pre_roll"] = True
    return info


def _kf(points: list[tuple[float, float, str]], start: float, lane: str) -> list[dict]:
    """Keyframe dicts from (beat, value, curve) points, anchored at the window start; same-beat duplicates of the
    same value merge (the later curve wins)."""
    pts = sorted(points, key=lambda p: p[0])
    if not pts:
        return [{"beat": start, "value": LANE_DEFAULTS[lane], "curve": "hold"}]
    if len(pts) == 1:  # a constant lane
        return [{"beat": start, "value": round(float(pts[0][1]), 4), "curve": "hold"}]
    if pts[0][0] > start:
        pts.insert(0, (start, pts[0][1], "hold"))
    out: list[dict] = []
    for beat, value, curve in pts:
        k = {"beat": round(float(beat), 4), "value": round(float(value), 4), "curve": curve}
        if out and abs(out[-1]["beat"] - k["beat"]) < 1e-9 and abs(out[-1]["value"] - k["value"]) < 1e-9:
            out[-1] = k
        else:
            out.append(k)
    return out


# --- planning: notes -----------------------------------------------------------------------------------------------


def _mmss(g: _Grid, bar: float) -> str:
    return transitions._mmss(g.fd + bar * g.bar_s)


_plural = transitions._plural


def _headline(m: _Mix, tech: str, bars: int, place: _Place, info: dict) -> str:
    ga, gb = m.ga, m.gb
    a0, b0, L = place.a0, place.b0, place.overlap
    t0, tb, te = _mmss(ga, a0), _mmss(gb, b0), _mmss(ga, a0 + L)
    if m.real_a and tech not in (*_CUTS, "loop roll") and (lp := transitions._a_loop(ga, a0, L)) is not None:
        te = f"the end of its {lp.bars}-bar loop"
    swap = f"swap the bass on the 1 of bar {place.swap + 1} (A {_mmss(ga, a0 + place.swap)})" \
        if place.swap is not None else None
    if tech == "long blend":
        what = f"{L}-bar long blend: B in at A {t0} (bar {a0 + 1}, on the phrase) from B {tb}, " \
               f"{swap or 'B brings its lows in as A leaves'}, A out by {te}"
    elif tech in ("bass swap", "phrase blend"):
        what = f"{L}-bar {tech}: B in at A {t0} (bar {a0 + 1}) from B {tb}, " + \
               (f"{swap}, A out by {te}" if swap else f"A's bass carries the blend and is cut on the 1 at {te}")
    elif tech == "filter sweep":
        what = f"{L}-bar filter sweep: B opens up from a closed low-pass under A from A {t0} (bar {a0 + 1}) while " \
               f"A rides a high-pass sweep out by {te}" + (f"; {swap}" if swap else "")
    elif tech == "echo out" and L:
        what = f"{L}-bar blend from A {t0} (bar {a0 + 1}), " + (f"{swap}, " if swap else "") + \
               f"then echo A out on its last beat and cut it on the 1 at {te}"
    elif tech == "echo out":
        what = f"echo out: throw A's last 2 beats into a dotted-8th echo and cut A on the 1 at {t0} (bar {a0 + 1}); " \
               f"B starts on that 1 from B {tb}"
    elif tech == "loop roll":
        lp = info.get("loop")
        looped = ""
        if lp:
            size = int(lp["length_beats"] // 4)
            s0 = a0 + int(lp["source_beat"] // 4)
            looped = f"A's last {size} bars ({_mmss(ga, s0)}–{_mmss(ga, s0 + size)}) loop so B's intro can play " \
                     f"out under it, "
        what = f"{L}-bar loop roll: B in at A {t0} (bar {a0 + 1}) from B {tb}; {looped}then A rolls 1 bar → " \
               f"2 beats → 1 → ½ and is cut on the 1 as B's bass drops in"
    elif tech == "slam":
        into = "its drop" if b0 in gb.drops else "its main section" if b0 == gb.intro[1] else "its cue"
        at = f"A {t0} (bar {a0 + 1})"
        frm = f"the end of A's build, {at}," if a0 == _a_build_end(m) else at
        riser = "a short high-pass riser" if b0 in gb.drops else \
            f"a high-pass riser over its last {max(2, min(bars, 8))} bars"
        roll = ", rolling its last 2 bars 1 bar → 2 beats → 1 → ½" if info.get("roll") else ""
        pre = "; B's last build bar creeps in under A's last bar" if info.get("pre_roll") else ""
        what = f"slam: A rides {riser}{roll}{pre}; on the 1 at {frm} A is cut into an " \
               f"echo/reverb throw and B slams in at {into} (B {tb}, bar {b0 + 1})"
    elif tech == "reverb wash":
        what = f"reverb wash: A's reverb swells over its last {max(4, min(bars, 8))} bars under a rising high-pass, " \
               f"then on the 1 at A {t0} (bar {a0 + 1}) A cuts into B (from B {tb}) and the wash rings over it"
    elif tech == "drop swap":
        what = f"drop swap: let A's build run and on the 1 where A would drop (A {t0}, bar {a0 + 1}) cut into " \
               f"B's drop at {tb}, a short reverb wash on A's last beat"
    else:
        what = f"quick cut on the 1 at A {t0} (bar {a0 + 1}) into B from {tb}"
    edges = ""
    if m.tr is not None and m.tr.get("notes"):
        first = str(m.tr["notes"][0])
        if " → " in first:
            edges = first.split(" → ", 1)[0] + " → "
    text = edges + what + "."
    return text[0].upper() + text[1:]


def _eq_note(tech: str, place: _Place) -> str | None:
    L = place.overlap
    if tech in ("long blend", "bass swap", "phrase blend"):
        fade_in, i_end, o_start = (round(x / 4) for x in _phrases(L, place.swap))
        if place.swap is not None:
            until = "the swap"
            out = f"{_plural(L - o_start, 'bar')} take A out — highs, then mids, then the fader — under a slowly " \
                  f"rising high-pass"
        else:
            until = "A's last phrase"
            out = f"{_plural(L - o_start, 'bar')} ease A's highs and mids down while its bass carries to the 1 " \
                  f"where B's lows come in"
        ride = f"both ride together through bars {i_end + 1}–{o_start}; the last {out}" if o_start > i_end else \
            f"from bar {i_end + 1} both ride together while the last {out}"
        return f"In phrases: bars 1–{i_end} bring B in with its lows killed behind a gentle filter (fader up over " \
               f"{_plural(fade_in, 'bar')}, highs opening, mids held 4 dB down until {until}); {ride}; A's reverb " \
               f"swells over its last 2 bars and its echo over the last, both still fed a beat past the cut so they " \
               f"ring over B."
    if tech == "filter sweep":
        return "Classic filter crossfade: A's high-pass climbs over the whole blend (echo and reverb at the top), " \
               "B's low-pass opens over the first half, the bass swaps on the 1."
    if tech == "echo out" and L:
        return "A keeps its fader up under a slowly rising high-pass; a dotted-8th echo (feedback 0.7) catches " \
               "its last 2 beats and rings over B for a few bars after the cut."
    if tech == "loop roll":
        return "B stays bass-free until the roll ends; during the roll A's lows go and a high-pass rises, and a " \
               "dotted-8th echo catches its last half beat."
    return None


# --- public API ----------------------------------------------------------------------------------------------------


def generate(
    a: Track,
    a_an: dict | None,
    b: Track,
    b_an: dict | None,
    folder: str,
    technique: str | None = None,
    bars: int | None = None,
    transition: dict | None = None,
    mix_length: str = "normal",
) -> dict:
    """MixPlan (types.ts) for A → B: the engine's (or the requested) technique/blend length as beat-locked lane
    automation, tempo moves and a preview window. Works without analyses (rough plan from tag BPMs). mix_length
    (short/normal/long, settings.mix_length) is the preferred blend length when `bars` isn't given: 8, 16 or 32."""
    if technique is not None and technique not in TECHNIQUES:
        raise ValueError(f"unknown technique {technique!r}")
    if bars is not None and bars not in BARS:
        raise ValueError(f"bars must be one of {BARS}")
    m = _mix(a, a_an, b, b_an, transition, mix_length)
    eng = _engine(m)
    e_tech, e_place = eng
    tech = technique or _auto_technique(m, e_tech, e_place)
    notes_extra: list[str] = []

    big_gap = abs(m.b_bpm / m.a_bpm - 1) > _MAX_BLEND_GAP
    bars_given = bars is not None
    if tech in _CUTS or (tech == "echo out" and bars is None and (e_place.overlap == 0 or big_gap)):
        place = _cut_place(m, tech, eng)
        bars = bars or (8 if tech == "reverb wash" else 4)
    else:
        bars = bars or _default_bars(m, tech, e_place)
        if tech == "loop roll":
            if e_place.overlap and e_tech not in _CUTS:
                place = _Place(e_place.a0, e_place.b0, bars, None)
            else:
                cut = _cut_place(m, "quick cut", eng)
                place = _Place(cut.a0, cut.b0, bars, None)
        else:
            place = _blend_place(m, bars, eng)
            place, note = _clear_a_drop(m, place)
            notes_extra += [note] if note else []
            place, note = _land_on_drop(m, place, fixed_len=bars_given)
            if note:
                notes_extra.append(note)
                if tech == "bass swap":
                    tech = "long blend" if place.overlap >= 16 else "phrase blend"
                bars = place.overlap
            if place.a0 + place.overlap > m.ga.end and m.real_a and \
                    transitions._a_loop(m.ga, place.a0, place.overlap) is None:
                notes_extra.append(f"A runs out {place.a0 + place.overlap - m.ga.end} bars before the blend ends — "
                                   "try a loop roll or a shorter blend.")
    L = place.overlap
    E = 4.0 * L
    ga, gb = m.ga, m.gb
    blend = bool(L) and tech not in (*_CUTS, "loop roll")
    a_loop = transitions._a_loop(ga, place.a0, L) if blend and m.real_a else None
    b_loop = place.b_loop if blend else None
    xb = b_loop.bars if b_loop else 0

    a_deck = {
        "track_id": a.id, "beat0_s": round(ga.fd + place.a0 * ga.bar_s, 4), "native_bpm": m.a_bpm,
        "play_from_beat": 0.0, "play_to_beat": E, "echo_beats": 0.75, "loops": [], "lanes": {},
    }  # fmt: skip
    if m.a_dur > 0:  # A plays on (fader down) into its sends after it is out, while it has audio
        a_end = (m.a_dur - a_deck["beat0_s"]) * m.a_bpm / 60.0
        a_deck["play_to_beat"] = round(max(E, min(E + _SEND_TAIL, a_end)), 4)
    else:
        a_deck["play_to_beat"] = E + _SEND_TAIL
    # a B loop holds B's grid back by its length: B's cue bar sounds on the master bar where the loop releases
    b_beat0 = gb.fd + (place.b0 - xb) * gb.bar_s
    b_deck = {
        "track_id": b.id, "beat0_s": round(b_beat0, 4), "native_bpm": m.b_bpm,
        "play_from_beat": 0.0 if xb else max(0.0, round(-b_beat0 * m.b_bpm / 60.0, 4)), "play_to_beat": 0.0,
        "echo_beats": 0.75, "loops": [], "lanes": {},
    }  # fmt: skip
    if a_loop is not None:  # slip loop from where A's music runs out until it leaves
        a_deck["loops"] = [{"start_beat": 4.0 * a_loop.at, "end_beat": E, "source_beat": 4.0 * (a_loop.src - place.a0),
                            "length_beats": 4.0 * a_loop.size}]
    if b_loop is not None:  # B's intro phrase from its cue, released on the phrase where its grid catches up
        b_deck["loops"] = [{"start_beat": 0.0, "end_beat": 4.0 * xb, "source_beat": 4.0 * (b_loop.src - place.b0 + xb),
                            "length_beats": 4.0 * b_loop.size}]
    a_deck["play_from_beat"] = round(min(-1.0, -a_deck["beat0_s"] * m.a_bpm / 60.0), 4)
    b_end_s = m.b_dur if m.b_dur > 0 else gb.fd + gb.n * gb.bar_s
    b_deck["play_to_beat"] = round(max(E + 8.0, (b_end_s - b_beat0) * m.b_bpm / 60.0), 4)

    A, B = _lanes(), _lanes()
    if tech == "slam":
        info = _slam_lanes(m, place, bars, A, B, a_deck, b_deck)
    elif tech in _CUTS or (tech == "echo out" and L == 0):
        info = _cut_lanes(tech, bars, A, place.b0 in gb.drops)
    elif tech == "loop roll":
        info = _loop_roll(m, place, A, B, a_deck)
    else:
        info = _blend_lanes(tech, L, place.swap, A, B, _mid_throws(m, place))
    clash = _clash_bars(m, a_deck, b_deck, place) if tech not in ("filter sweep",) else []
    ducked = _duck_mids(A, B, clash, place.swap if tech != "loop roll" else None, L) if clash and L else ([], [])
    a_deck["echo_beats"] = info["echo_beats"]
    if "reverb_size" in info:
        a_deck["reverb_size"] = info["reverb_size"]
    if info["feedback"] != LANE_DEFAULTS["echo_feedback"]:
        A["echo_feedback"] = [(0.0, info["feedback"], "hold")]

    tempo = _tempo(m, E, cut=L == 0)
    if tech == "slam" and tempo.keyframes[-1][0] <= E and tempo.keyframes[-1][1] != m.a_bpm:
        a_deck["play_to_beat"] = E  # a slam jumps to B's tempo on the 1: A's tail would be time-stretched, so no tail

    # preview window: 8-16 bars before B, 8-16 bars after A is out, ~90 s at most (pre-ramps shown when they fit)
    pre, post = (16 if L <= 8 else 8), (16 if L == 0 else 8)
    if tempo.pre_start is not None:
        pre = max(pre, int(-tempo.pre_start // 4))
    tk = [{"beat": bt, "value": v, "curve": c} for bt, v, c in tempo.keyframes]
    lo_beat = math.ceil(a_deck["play_from_beat"])
    hi_beat = math.floor(b_deck["play_to_beat"])

    def bounds(pre_bars: int, post_bars: int) -> tuple[float, float]:
        return max(lo_beat, -4.0 * pre_bars), min(hi_beat, E + 4.0 * post_bars)

    def secs(pre_bars: int, post_bars: int) -> float:
        s, e = bounds(pre_bars, post_bars)
        return master_seconds(tk, e, s)

    while secs(pre, post) > _MAX_PREVIEW_S:
        if pre > 8:
            pre -= 4
        elif post > 8:
            post -= 4
        elif post > 4:
            post -= 4
        elif pre > 4:
            pre -= 4
        elif pre > 2 or post > 2:
            pre, post = max(2, pre - 2), max(2, post - 2)
        else:
            break
    ws, we = bounds(pre, post)

    lanes_a = {lane: _kf(A[lane], ws, lane) for lane in LANES}
    lanes_b = {lane: _kf(B[lane], ws, lane) for lane in LANES}
    a_deck["lanes"], b_deck["lanes"] = lanes_a, lanes_b
    tempo_kf = _kf(tempo.keyframes, ws, "volume")
    for k in tempo_kf:
        k["value"] = round(k["value"], 3)

    markers = [{"beat": 0.0, "label": "B in" if L else "cut"}]
    if L and tech != "loop roll" and place.swap is not None:
        markers.append({"beat": 4.0 * place.swap, "label": "bass swap"})
    if tech == "loop roll":
        if info.get("loop"):
            markers.append({"beat": info["loop"]["start_beat"], "label": "loop"})
        markers.append({"beat": E - 8.0, "label": "loop roll"})
        markers.append({"beat": E, "label": "bass swap"})
    if info.get("roll"):
        markers.append({"beat": -8.0, "label": "loop roll"})
    if info.get("pre_roll"):
        markers.append({"beat": -4.0, "label": "B pre-roll"})
    for t in [*info.get("throws", []), *([info["throw"]] if info["throw"] is not None else [])]:
        markers.append({"beat": t, "label": "echo throw"})
    if tech == "reverb wash":
        markers.append({"beat": -4.0 * max(4, min(bars, 8)), "label": "reverb wash"})
    if a_loop is not None:
        markers.append({"beat": 4.0 * a_loop.at, "label": "A loop"})
    if b_loop is not None:
        markers.append({"beat": 4.0 * xb, "label": "B loop out"})
    if L:
        markers.append({"beat": E, "label": "A out"})
    if tempo.pre_start is not None:
        markers.append({"beat": tempo.pre_start, "label": "tempo ramp"})
    if tempo.post_end is not None:
        markers.append({"beat": E, "label": "tempo ramp"})

    notes = [_headline(m, tech, bars, place, info)]
    if (eq := _eq_note(tech, place)) is not None:
        notes.append(eq)
    if clash:
        k0, k1 = clash[0], clash[-1] + 1
        where = f"bar {k0 + 1}" if k1 == k0 + 1 else f"bars {k0 + 1}–{k1}"
        on_b, on_a = ducked
        who = []
        if on_b:
            span = f"bar {on_b[0] + 1}" if len(on_b) == 1 else f"bars {on_b[0] + 1}–{on_b[-1] + 1}"
            who.append(f"B's mids are ducked to {_VOCAL_CUT:.0f} dB through {span} while A leads")
        if on_a:
            who.append(f"A's mids are ducked to {_VOCAL_CUT:.0f} dB from bar {on_a[0] + 1} on its way out")
        notes.append(f"Vocals on both decks in {where} of the blend — {' and '.join(who)}, so the blend keeps its "
                     "length.")
    if info.get("throws"):
        bars_at = " and ".join(f"bar {int(t + 1) // 4 + 1}" for t in info["throws"])
        notes.append(f"Short echo throws on A into {bars_at} of the blend (the end of a vocal phrase or a breakdown "
                     "exit): the send opens on the last beat and the repeats ring over B.")
    if b_loop is not None:
        notes.append(f"B's intro is stretched: B loops its bars {b_loop.src + 1}–{b_loop.src + b_loop.size} "
                     f"({b_loop.size} bars) from B {_mmss(gb, place.b0)} as it comes in and releases the loop on the 1 "
                     f"of bar {xb + 1} (a phrase), so its intro then runs as A leaves.")
    if a_loop is not None:
        notes.append(f"A's outro is stretched: A's music runs out {_plural(a_loop.bars, 'bar')} before the blend "
                     f"ends, so from bar {a_loop.at + 1} A loops its bars {a_loop.src + 1}–{a_loop.src + a_loop.size} "
                     f"({a_loop.size} bars, {_mmss(ga, a_loop.src)}) until it leaves on the phrase.")
    notes.append(tempo.note)
    notes.extend(notes_extra)
    if not (m.real_a and m.real_b):
        missing = " and ".join(n for n, ok in (("A", m.real_a), ("B", m.real_b)) if not ok)
        notes.append(f"Rough plan from tag BPMs: {missing} {'is' if ' and ' not in missing else 'are'} not "
                     "analysed yet, so beats and phrases may not line up.")

    plan = {
        "version": PLAN_VERSION, "folder": folder, "from_id": a.id, "to_id": b.id, "technique": tech,
        "bars": bars, "key_lock": True, "window": {"start_beat": ws, "end_beat": we}, "tempo": tempo_kf,
        "a": a_deck, "b": b_deck, "markers": markers, "notes": notes, "edited": False,
    }  # fmt: skip
    return validate(plan)


# --- summary -------------------------------------------------------------------------------------------------------


def summary(plan: dict, a_an: dict | None = None, b_an: dict | None = None) -> str:
    """Text digest of a plan (technique, bars, cue bars, swap bar, tempo, markers, every automated lane) for the
    pair-render script and the snapshot test. Cue bars count from each deck's first downbeat (0 s without analysis)."""

    def cue(deck: dict, an: dict | None) -> str:
        """The deck's bar at master beat 0, through a loop engaged on that 1 (B looping its intro from its cue)."""
        fd = _num((an or {}).get("first_downbeat_s"))
        t0 = round(float(deck_time({**deck, "loops": [lp for lp in deck["loops"] if lp["start_beat"] == 0.0]}, 0.0)), 3)
        t0 += 0.0  # no "-0.00 s"
        bar = (t0 - fd) * deck["native_bpm"] / 240.0
        return f"bar {bar:g} ({t0:.2f} s)" if abs(bar - round(bar)) > 1e-3 else f"bar {round(bar)} ({t0:.2f} s)"

    def effects(name: str, deck: dict) -> list[str]:
        """One line per lane that leaves its default: keyframes as value@beat + curve initial (l/h/e)."""
        out = []
        for lane in LANES:
            kfs = deck["lanes"][lane]
            if any(k["value"] != LANE_DEFAULTS[lane] for k in kfs):
                out.append(f"  {name} {lane}: " + " ".join(f"{k['value']:g}@{k['beat']:g}{k['curve'][0]}" for k in kfs))
        sends = [f"{k} {deck[k]}" for k in ("echo_tap", "reverb_tap") if deck.get(k, SEND_TAPS[0]) != SEND_TAPS[0]]
        if deck.get("reverb_size", REVERB_SIZE) != REVERB_SIZE:
            sends.append(f"reverb_size {deck['reverb_size']:g}")
        if sends:
            out.append(f"  {name} sends: " + ", ".join(sends))
        for lp in deck["loops"]:
            out.append(f"  {name} loop: beats {lp['start_beat']:g}..{lp['end_beat']:g} repeat "
                       f"{lp['length_beats']:g} from {lp['source_beat']:g}")
        return out or [f"  {name}: no effects"]

    tempo = [round(k["value"], 1) for k in plan["tempo"]]
    tempo = [t for i, t in enumerate(tempo) if i == 0 or t != tempo[i - 1]]
    swap = next((m["beat"] for m in plan["markers"] if m["label"] == "bass swap"), None)
    marks = " ".join(f"{m['label'].replace(' ', '_')}@{m['beat']:g}" for m in plan["markers"])
    w = plan["window"]
    return "\n".join([
        f"{plan['technique']}, {plan['bars']} bars, swap {'-' if swap is None else f'bar {swap / 4 + 1:g}'}",
        f"  cue: A {cue(plan['a'], a_an)} / B {cue(plan['b'], b_an)}",
        f"  tempo: {' -> '.join(f'{t:g}' for t in tempo)} BPM, window {w['start_beat']:g}..{w['end_beat']:g} beats",
        f"  markers: {marks}",
        *effects("A", plan["a"]),
        *effects("B", plan["b"]),
    ])  # fmt: skip
