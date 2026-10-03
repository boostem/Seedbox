"""Mix plans (APP_SPEC.md v4): the automation script of one A → B transition, which mixrender turns into audio.

Everything lives on one MASTER timeline in beats. Beat 0 is where B comes in, on A's phrase grid (the placement of
transitions.suggest). Each deck maps master beats to its own file time through its beat grid (deck_time, loops
honoured) and is time-stretched with key lock so its beats land on the master clock, whose BPM follows `tempo`.
Lanes (fader, 3-band isolator EQ, bipolar filter, pre-fader echo/reverb sends) are keyframed in master beats, and
lane_value is the one interpolation the renderer, the UI and the tests share.

A master bar is 4 master beats: A bar a0 + k and B bar b0 + k sound together in master bar k (a half/double-time B
is put on A's grid first, as in transitions, so its native_bpm is the analysis BPM x 2 or x 0.5). `bars` is the
blend length; for the cuts (slam, quick cut, drop swap, brake, spinback, an echo out without overlap) it is the
length of A's lead-in effect (filter riser / loop roll) before the cut on the 1.

Loops do real work: A's last clean phrase loops to stretch a short outro into a long blend, B's intro loops to hold
its groove under A's long outro (its main section then lands as A leaves), and loop rolls (8 -> 4 -> 2 -> 1 -> 1/2
beat) build into the cuts. Echo throws catch A's vocal phrase ends and the bass swap mid-blend, reverb washes ride
filter-sweep peaks, breakdown exits and drops; deck FX (turntable brake, spinback) end the hard cuts.
"""

from __future__ import annotations

import copy
import hashlib
import math
from dataclasses import dataclass

import numpy as np

from . import mixing, transitions
from .models import Track

PLAN_VERSION = 3

LANES = ("volume", "eq_low", "eq_mid", "eq_high", "filter", "echo_send", "echo_feedback", "reverb_send")
LANE_RANGES: dict[str, tuple[float, float]] = {
    "volume": (0.0, 1.0), "eq_low": (-30.0, 6.0), "eq_mid": (-30.0, 6.0), "eq_high": (-30.0, 6.0),
    "filter": (-1.0, 1.0), "echo_send": (0.0, 1.0), "echo_feedback": (0.0, 0.95), "reverb_send": (0.0, 1.0),
}  # fmt: skip
LANE_DEFAULTS: dict[str, float] = {
    "volume": 1.0, "eq_low": 0.0, "eq_mid": 0.0, "eq_high": 0.0, "filter": 0.0,
    "echo_send": 0.0, "echo_feedback": 0.55, "reverb_send": 0.0,
}  # fmt: skip
CURVES = ("linear", "hold", "ease")
TECHNIQUES = (*transitions.TECHNIQUES, "filter sweep", "loop roll", "slam", "brake", "spinback")
BARS = (4, 8, 16, 32)
TEMPO_RANGE = (40.0, 300.0)
ECHO_BEATS_RANGE = (0.125, 4.0)
FX_KINDS = ("brake", "spinback")
FX_BEATS_RANGE = (0.25, 16.0)

_CUTS = ("drop swap", "quick cut", "slam", "brake", "spinback")
_EXTENDABLE = ("long blend", "phrase blend", "bass swap")
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
_MAX_FX = 8
_MAX_ABS_BEAT = 100_000.0
_MIN_LOOP_BEATS = 1 / 16
_VOCAL_CUT = -18.0  # dB on A's mids while vocals overlap
_HOLD_MIN = 8  # bars of B's main section under A before B's intro is held instead
_THROW_ECHO = 0.7  # echo send of a throw on A's vocal phrase end
_SWAP_ECHO = 0.45  # echo accent on A's last beat before the bass swap

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
        "loops": _loops(raw.get("loops"), what), "fx": _fx(raw.get("fx"), what), "lanes": lanes,
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
    b0: int  # B's cue: the B bar it starts from at master bar 0
    overlap: int  # bars both decks play (0 = cut on the 1 at master beat 0)
    swap: int | None  # 0-based overlap bar where B's low end replaces A's; None = B's lows come in as A leaves
    hold: int = 0  # master bars B's first hold_size bars loop before B plays on from b0 (B intro hold)
    hold_size: int = 0


def _b_bar(p: _Place, k: int) -> int:
    """B bar sounding at master bar k (B's intro hold honoured)."""
    if p.hold and k < p.hold:
        return p.b0 + k % max(1, p.hold_size)
    return p.b0 + k - p.hold


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
    a_breaks: tuple[int, ...] = ()  # A bars where a breakdown ends (the drop / build hits)
    b_breaks: tuple[int, ...] = ()


def _fake_grid(bpm: float, duration_s: float) -> _Grid:
    """Bar grid from the BPM alone (no analysis): bar 0 at 0 s, 8-bar phrases, no known structure."""
    bar_s = 240.0 / bpm
    n = max(24, int(duration_s / bar_s)) if duration_s > 0 else 128
    return _Grid(
        fd=0.0, bar_s=bar_s, n=n, vocal=(0.0,) * n, low=(0.6,) * n, energy=(0.7,) * n, perc=(False,) * n,
        intro=(0, 0), outro=(n, n), intro_drums=False, intro_vocals=False, outro_drums=False, outro_vocals=False,
        phrases=tuple(range(0, n + 1, _PHRASE)), drops=(), end=n, off=0,
    )  # fmt: skip


def _mix(a: Track, a_an: dict | None, b: Track, b_an: dict | None, transition: dict | None) -> _Mix:
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
        tr = transition if ok else transitions.suggest(a, a_an, b, b_an)
    real_a, real_b = ga is not None, gb is not None
    ga, gb = ga or _fake_grid(a_bpm, a_dur), gb or _fake_grid(b_bpm * factor, b_dur)
    return _Mix(
        a=a, b=b, ga=ga, gb=gb, real_a=real_a, real_b=real_b, a_bpm=a_bpm, b_bpm=b_bpm * factor,
        b_raw_bpm=b_bpm, factor=factor, move=mixing._camelot_move(a.camelot, b.camelot), tr=tr, a_dur=a_dur,
        b_dur=b_dur, a_breaks=_breaks(a_an, ga), b_breaks=_breaks(b_an, gb),
    )  # fmt: skip


def _breaks(an: dict | None, g: _Grid) -> tuple[int, ...]:
    """Bars (on g) where a breakdown section ends."""
    out = set()
    for s in (an or {}).get("sections") or []:
        if isinstance(s, dict) and s.get("kind") == "breakdown" and _finite(s.get("end_s")):
            k = round((float(s["end_s"]) - g.fd) / g.bar_s)
            if 0 < k < g.n:
                out.add(k)
    return tuple(sorted(out))


def _variety(m: _Mix) -> int:
    """Stable per-pair number so a set doesn't end every hard cut the same way."""
    return int(hashlib.md5(f"{m.a.id}>{m.b.id}".encode()).hexdigest()[:8], 16)


def _mean(arr: tuple[float, ...], lo: int, hi: int) -> float:
    vals = arr[max(0, lo) : max(0, hi)]
    return sum(vals) / len(vals) if vals else 0.0


def _swap(gb: _Grid, b0: int, length: int) -> int | None:
    if length <= 0:
        return None
    return transitions._swap_offset(gb, transitions._Plan("", 0, b0, length, 0.0, []))


# --- planning: placement -------------------------------------------------------------------------------------------


def _engine(m: _Mix) -> tuple[str, _Place]:
    """The suggestion engine's technique and bars; without both analyses a plain 8-bar bass swap near A's end."""
    ga, gb, tr = m.ga, m.gb, m.tr
    if tr is not None and tr.get("ready") and tr.get("technique") in transitions.TECHNIQUES:
        a0 = max(0, min(ga.n, round((tr["out_start_s"] - ga.fd) / ga.bar_s)))
        b0 = max(0, min(gb.n - 1, round((tr["in_start_s"] - gb.fd) / gb.bar_s)))
        length = int(_num(tr.get("overlap_bars")))
        sw = tr.get("swap_bar")
        swap = sw - 1 if isinstance(sw, int) and 0 < sw <= length else None
        return tr["technique"], _Place(a0, b0, length, swap)
    return "bass swap", _fallback_place(m, 8)


def _fallback_place(m: _Mix, length: int) -> _Place:
    """B in on A's phrase so the blend ends a phrase before A's end (or at its outro end when A is analysed)."""
    ga, gb = m.ga, m.gb
    end = ga.end if m.real_a else max(length, ga.end - _PHRASE)
    fits = [p for p in ga.phrases if p + length <= end]
    a0 = fits[-1] if fits else max(0, end - length)
    b0 = transitions._first_cue(gb) if m.real_b else 0
    return _Place(a0, b0, length, _swap(gb, b0, length))


def _best_of_length(m: _Mix, length: int) -> _Place | None:
    """Best phrase-aligned overlap of exactly `length` (8/16/32) bars, scored by transitions' own window scorer."""
    ga, gb = m.ga, m.gb
    i0, i1 = gb.intro
    cues = [p for p in gb.phrases if p <= max(gb.phrases[0], i1 - _PHRASE)] if gb.phrases else [i0]
    best = None
    for a0 in ga.phrases:
        if a0 < ga.end // 2 or a0 + length > ga.end or a0 + length < ga.end - transitions._MAX_CUT_BARS:
            continue
        for b0 in cues:
            if b0 + length <= gb.n:
                w = transitions._window(ga, gb, a0, b0, length, m.move == "clash")
                if best is None or w.fit > best.fit + 1e-9:
                    best = w
    return None if best is None else _Place(best.a0, best.b0, length, _swap(gb, best.b0, length))


def _blend_place(m: _Mix, length: int, eng: tuple[str, _Place]) -> _Place:
    e_tech, e = eng
    if length == 4:
        base = _blend_place(m, 8, eng)
        return _Place(base.a0 + 4, base.b0 + 4, 4, _swap(m.gb, base.b0 + 4, 4))
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
    if technique in ("slam", "spinback"):
        # cut where A's outro starts (on its phrase) straight into B's first drop / main section
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
    return _Place(cands[0], place.b0, place.overlap, place.swap), \
        f"B comes in a phrase into A's last drop (A bar {cands[0] + 1}) rather than over the breakdown before it."


def _land_on_drop(m: _Mix, place: _Place, fixed_len: bool) -> tuple[_Place, str | None]:
    """When B goes <intro> <chill> <drop> and the blend would end in the chill, end it on B's drop: lengthen it to
    16/32 bars over A's outro when A has the room (and the length isn't fixed), else cue B later so the overlap runs
    through the chill; either way A rides to the drop and the lows change hands on its 1."""
    if not (m.real_a and m.real_b) or not place.overlap:
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


def _clean(g: _Grid, lo: int, hi: int) -> bool:
    """Bars [lo, hi) carry no real vocal (fine to loop)."""
    return hi > lo and _mean(g.vocal, lo, hi) < _LOOP_VOCAL_MAX


def _hold(m: _Mix, place: _Place, want: int) -> _Place | None:
    """B's intro looped for `want` master bars (rounded to its loop size) so its main section lands `want` bars
    later; None when B's intro has no clean 4/8 bars at its cue."""
    gb = m.gb
    intro = gb.intro[1] - place.b0
    for size in (8, 4):
        h = want // size * size
        if h >= size and intro >= size and _clean(gb, place.b0, place.b0 + size) and h <= place.overlap - 4:
            sw = _swap(gb, place.b0 - h, place.overlap)
            return _Place(place.a0, place.b0, place.overlap, sw, h, size)
    return None


def _extend(m: _Mix, tech: str, place: _Place, fixed: bool) -> tuple[_Place, str | None]:
    """Stretch a short in-key blend to 16 bars with loops: A's last clean phrase loops when A runs out under a long
    clean intro of B; B's intro loops when A has a long clean outro and B's intro is short."""
    if fixed or tech not in _EXTENDABLE or not (m.real_a and m.real_b) or m.move not in _GOOD_KEYS:
        return place, None
    if place.overlap >= 16 or abs(m.b_bpm / m.a_bpm - 1) > _SMALL_GAP:
        return place, None
    ga, gb = m.ga, m.gb
    L = 16
    b_intro = gb.intro[1] - place.b0
    ends_at_a_end = ga.end - (place.a0 + place.overlap) <= 1
    if ends_at_a_end and b_intro >= L and _clean(gb, place.b0, place.b0 + L):
        blk = _loop_block(m, place.a0, L)
        if blk is not None and _clean(ga, blk[0] - blk[1], blk[0]):
            new = _Place(place.a0, place.b0, L, _swap(gb, place.b0, L))
            return new, f"A's outro is short, so its last {blk[1]} clean bars loop to stretch the blend to {L} bars " \
                        f"while B's {b_intro}-bar intro plays out under it."
    a0 = ga.end - L
    if ends_at_a_end and a0 >= max(ga.outro[0], ga.end // 2) and a0 in ga.phrases and _clean(ga, a0, ga.end) \
            and 4 <= b_intro < L:
        held = _hold(m, _Place(a0, place.b0, L, None), L - b_intro)
        if held is not None:
            return held, f"A's {ga.end - ga.outro[0]}-bar outro is long and B's intro is short: B comes in {L} bars " \
                         f"before A's end and its first {held.hold_size} bars loop for {held.hold} bars, so B's main " \
                         f"section lands right as A leaves."
    return place, None


def _auto_hold(m: _Mix, tech: str, place: _Place) -> tuple[_Place, str | None]:
    """When B's main section would run under A for a phrase or more, hold B's intro instead (looped)."""
    if place.hold or not place.overlap or not m.real_b or tech in ("loop roll", "echo out"):
        return place, None
    under = place.b0 + place.overlap - max(m.gb.intro[1], place.b0)
    if under < _HOLD_MIN or m.gb.intro[1] - place.b0 < 4:
        return place, None
    held = _hold(m, place, under)
    if held is None:
        return place, None
    return held, f"B's first {held.hold_size} bars loop for {held.hold} bars at the start so its main section " \
                 f"doesn't run under A ({under} bars otherwise)."


def _stretch(m: _Mix, place: _Place) -> dict | None:
    """Loop over A's last clean bars when its music ends before the blend does (any blend, any length)."""
    if not (m.real_a and place.overlap) or place.a0 + place.overlap <= m.ga.end:
        return None
    blk = _loop_block(m, place.a0, place.overlap + 2)
    if blk is None:
        return None
    q, size = blk
    start, src = 4.0 * (q - place.a0), 4.0 * (q - size - place.a0)
    end = 4.0 * place.overlap + 4.0
    if start >= end - 4.0:
        return None
    return {"start_beat": start, "end_beat": end, "source_beat": src, "length_beats": 4.0 * size}


def _roll(at: float, bars: int, src: float | None = None) -> list[dict]:
    """Loop roll ending on master beat `at`: 8 -> 4 -> 2 -> 1 -> 1/2 beat windows (4 bars), 4 -> 2 -> 1 -> 1/2
    (2 bars) or 2 -> 1 -> 1/2 -> 1/4 (1 bar), every window starting on the roll's first beat (`src`)."""
    stages = {4: (8, 4, 2, 1, 0.5), 2: (4, 2, 1, 0.5), 1: (2, 1, 0.5, 0.25)}[bars]
    r = at - 4.0 * bars
    src = r if src is None else src
    out, b = [], r
    for i, ln in enumerate(stages):  # each window plays for its own length; the last one runs to the cut
        e = at if i == len(stages) - 1 else b + ln
        out.append({"start_beat": b, "end_beat": e, "source_beat": src, "length_beats": float(ln)})
        b = e
    return out


_HARD_EXITS = ("echo out", "echo out", "brake", "spinback")  # no blend possible: A leaves one of these ways
_CUT_EXITS = ("slam", "quick cut", "slam", "spinback")


def _auto_technique(m: _Mix, e_tech: str, e: _Place) -> str:
    """Planner technique for the engine's pick: when A runs out under a long clean intro of B, a long blend over
    A's looped drum outro (or a loop roll out of a tonal one); a filter sweep over awkward keys or a vocal outro;
    long blend for long in-key blends; hard exits varied per pair (echo out, brake, spinback, slam with a roll)."""
    gap = abs(m.b_bpm / m.a_bpm - 1)
    v = _variety(m)
    if e.overlap and gap > _MAX_BLEND_GAP:
        return _HARD_EXITS[v % 4]
    if e.overlap == 0:
        if not (m.real_a and m.real_b) or e_tech == "drop swap":
            return e_tech
        return _HARD_EXITS[v % 4] if e_tech == "echo out" else _CUT_EXITS[v % 4]
    if not (m.real_a and m.real_b):
        return e_tech
    ga, gb = m.ga, m.gb
    good_key = m.move in _GOOD_KEYS
    if e.overlap <= 8 and ga.end - (e.a0 + e.overlap) <= 1 and gb.intro[1] - e.b0 >= 16 and good_key \
            and gap <= _LOOP_ROLL_GAP and _mean(gb.vocal, e.b0, e.b0 + 16) < _LOOP_VOCAL_MAX:
        blk = _loop_block(m, e.a0, 16)
        if blk is not None and _mean(ga.vocal, blk[0] - blk[1], blk[0]) < _LOOP_VOCAL_MAX:
            return "long blend" if ga.outro_drums and gap <= _SMALL_GAP else "loop roll"
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
        return e.overlap if e.overlap in (16, 32) else 16
    return e.overlap if e.overlap in BARS else 8


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


_Pulse = tuple[float, float, float, float]


class _Sends:
    """Echo / reverb send pulses per deck, (start, peak, end, value): the send ramps from 0 at `start` to `value` at
    `peak`, holds to `end` and closes there (pre-fader, so the tail keeps ringing). Pulses are added in priority
    order; one that overlaps an earlier pulse on the same send is dropped."""

    def __init__(self) -> None:
        self.echo: dict[str, list[_Pulse]] = {"a": [], "b": []}
        self.verb: dict[str, list[_Pulse]] = {"a": [], "b": []}

    def add(self, kind: str, deck: str, start: float, peak: float, end: float, value: float) -> bool:
        pulses = (self.echo if kind == "echo" else self.verb)[deck]
        if end <= start or any(start < q[2] and end > q[0] for q in pulses):
            return False
        pulses.append((start, max(start, min(peak, end)), end, value))
        return True

    @staticmethod
    def lane(pulses: list[_Pulse]) -> list[tuple[float, float, str]]:
        pts: list[tuple[float, float, str]] = []
        for st, pk, en, v in sorted(pulses):
            pts += [(st, 0.0, "linear"), (pk, v, "hold"), (en, 0.0, "hold")]
        return pts


def _a_bar(m: _Mix, a_deck: dict, place: _Place, k: int) -> int:
    """A bar sounding at master bar k (A's loops honoured)."""
    return place.a0 + int(np.floor(float(_deck_beat(a_deck, 4.0 * k + 0.5)) / 4))


def _clash_bars(m: _Mix, a_deck: dict, place: _Place) -> list[int]:
    """Overlap bars (0-based) with vocals on both decks (A read through its loops, B through its intro hold)."""
    out = []
    for i in range(place.overlap):
        ab, bb = _a_bar(m, a_deck, place, i), _b_bar(place, i)
        va = m.ga.vocal[ab] if 0 <= ab < m.ga.n else 0.0
        vb = m.gb.vocal[bb] if 0 <= bb < m.gb.n else 0.0
        if va >= transitions._VOCAL_ON and vb >= transitions._VOCAL_ON:
            out.append(i)
    return out


def _vocal_ends(m: _Mix, a_deck: dict, place: _Place) -> list[float]:
    """Master beats (bar lines inside the blend) where one of A's sung phrases ends: echo-throw spots."""
    out = []
    for k in range(1, place.overlap):
        prev, cur = _a_bar(m, a_deck, place, k - 1), _a_bar(m, a_deck, place, k)
        if 0 <= prev < m.ga.n and 0 <= cur < m.ga.n and m.ga.vocal[prev] >= transitions._VOCAL_ON \
                and m.ga.vocal[cur] < transitions._VOCAL_FLOOR + 0.1:
            out.append(4.0 * k)
    return out


def _throw(S: _Sends, at: float, send: float = 0.9, reverb: float = 0.55) -> dict:
    """Echo throw into the cut on the 1 at `at`: A's echo send ramps up over its last 2 beats and is held through
    the cut (pre-fader, feedback 0.7: the repeats ring 2-4 bars over B), with a reverb wash over its last bar."""
    S.add("echo", "a", at - 2, at, at + 4, send)
    S.add("verb", "a", at - 4, at, at + 4, reverb)
    return {"echo_beats": 0.75, "feedback": 0.7, "throw": at - 2}


def _blend_lanes(tech: str, length: int, swap: int | None, A: dict, B: dict, S: _Sends, into_drop: bool) -> dict:
    """Blend automation spread over the whole overlap (same shape for 4/8/16/32 bars): B's fader comes up over the
    first quarter and its highs, mids and filter open over the first half; the bass swaps on the 1 of the swap bar
    with a 1-beat crossfade; A's mids/highs ease down over the second half under a high-pass rising from a quarter
    in; A's last bar goes into an echo throw and a reverb wash (pre-fader, so both ring out over B). Without a swap
    (B brings no bass, or A rides until B's drop) A's bass carries to the end and the lows change hands on the 1."""
    E = 4.0 * length
    sw = 4.0 * swap if swap is not None else None
    q = E / 4
    half = sw if sw is not None and sw <= 0.75 * E else E / 2
    info: dict = {"echo_beats": 0.75, "feedback": 0.6, "throw": None}
    B["volume"] = [(0.0, 0.0, "ease"), (max(4.0, q), 1.0, "hold")]
    B["eq_high"] = [(0.0, -12.0, "ease"), (half, 0.0, "hold")]
    B["eq_mid"] = [(0.0, -15.0, "ease"), (half, 0.0, "hold")]
    B["filter"] = [(0.0, -0.6 if tech == "filter sweep" else 0.4 if tech == "long blend" else 0.3, "ease"),
                   (half, 0.0, "hold")]
    if sw is not None:
        B["eq_low"] = [(0.0, _KILL, "hold"), (sw, _KILL, "linear"), (sw + 1, 0.0, "hold")]
        A["eq_low"] = [(0.0, 0.0, "hold"), (sw, 0.0, "linear"), (sw + 1, _KILL, "hold")]
    else:
        B["eq_low"] = [(0.0, _KILL, "hold"), (E, 0.0, "hold")]
    out0 = sw if sw is not None else E / 2
    sweep = 0.85 if tech == "filter sweep" else 0.35
    A["filter"] = [(0.0 if tech == "filter sweep" else q, 0.0, "ease"), (E, sweep, "hold")]
    if tech == "echo out":  # A's fader stays up; cut on the 1 under a long echo throw
        A["eq_mid"] = [(out0, 0.0, "ease"), (E, -6.0, "hold")]
        A["eq_high"] = [(out0, 0.0, "ease"), (E, -6.0, "hold")]
        A["volume"] = [(E - 4, 1.0, "hold"), (E, 0.0, "hold")]
        info.update(_throw(S, E))
        return info
    fade = max(8.0, q)
    if sw is None:  # A rides, bass and full energy, to the 1 where B's lows land (no dip before B's drop)
        A["eq_mid"] = [(out0, 0.0, "ease"), (E, -6.0, "hold")]
        A["eq_high"] = [(out0, 0.0, "ease"), (E, -6.0, "hold")]
        A["filter"] = [(E - 8, 0.0, "ease"), (E, 0.12, "hold")]
        A["volume"] = [(E - 4, 1.0, "hold"), (E, 0.0, "hold")]
    else:
        A["eq_mid"] = [(out0, 0.0, "ease"), (E, -8.0, "hold")]
        A["eq_high"] = [(out0, 0.0, "ease"), (E, -10.0, "hold")]
        A["volume"] = [(E - fade, 1.0, "ease"), (E, 0.0, "hold")]
    S.add("echo", "a", E - 4, E - 1, E + 4, 0.55 if sw is not None else 0.45)
    if tech == "filter sweep":  # the wash rides the sweep up to its peak
        S.add("verb", "a", E / 2, E, E + 4, 0.7)
    else:
        S.add("verb", "a", E - 8, E, E + 4, 0.65 if into_drop else 0.5)
    return info


def _cut_mids(A: dict, k0: int, E: float) -> float:
    """Vocals on both decks from overlap bar k0: duck A's mids to -18 dB over the 2 bars before (A is on its way out);
    a later tone-out ramp carries on from the ducked level. Returns the master beat where the duck lands."""
    at = max(8.0, 4.0 * k0)
    pts = [(at - 8.0, 0.0, "ease"), (at, _VOCAL_CUT, "hold")]
    ramp = [p for p in A["eq_mid"] if p[2] == "ease" and p[0] >= at]
    if ramp:
        r0 = ramp[0][0]
        r1 = next((p[0] for p in A["eq_mid"] if p[0] > r0), E)
        pts += [(r0, _VOCAL_CUT, "ease"), (r1, _VOCAL_CUT - 6.0, "hold")]
    A["eq_mid"] = pts
    return at


def _loop_roll(m: _Mix, place: _Place, A: dict, B: dict, a_deck: dict, S: _Sends) -> dict:
    """A loops its last bars if it runs out, then rolls 8 -> 4 -> 2 -> 1 -> 1/2 beat over its last 4 bars (16+ bar
    blends; 4 -> 2 -> 1 -> 1/2 over 2 bars otherwise) with its lows gone and a high-pass and reverb rising: a build
    that lands on the 1 where B's bass drops in, A's last half beat thrown into the echo."""
    length = place.overlap
    E = 4.0 * length
    rb = 4 if length >= 16 else 2
    R = E - 4.0 * rb  # roll start
    loops = []
    blk = _loop_block(m, place.a0, length)
    base = R
    if blk is not None:
        q, size = blk
        start, src, ln = 4.0 * (q - place.a0), 4.0 * (q - size - place.a0), 4.0 * size
        if R > start:
            loops.append({"start_beat": start, "end_beat": R, "source_beat": src, "length_beats": ln})
            base = src + math.fmod(R - start, ln)
    roll = _roll(E, rb, base)
    roll[-1]["end_beat"] = E + 2  # the last half-beat keeps stuttering into the echo after the cut
    loops += roll
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
    S.add("echo", "a", E - 1, E - 0.25, E + 4, 0.85)
    S.add("verb", "a", R, E, E + 4, 0.6)
    return {"echo_beats": 0.75, "feedback": 0.7, "throw": E - 1, "roll": (R, rb),
            "loop": loops[0] if blk and len(loops) > len(roll) else None}


def _cut_lanes(m: _Mix, tech: str, bars: int, A: dict, a_deck: dict, S: _Sends, place: _Place,
               into_drop: bool) -> dict:
    """Cuts on the 1 at master beat 0, B straight in at full. Brake: A powers down over its last bar. Spinback: A is
    spun back on its last 2 beats into a big wash. Into B's drop: a stutter on A's last bar, a short filter riser and
    a reverb wash, B's drop dry on the 1. Otherwise an echo throw on A, after a 4-bar loop roll (slam) or a 1-bar
    stutter (quick cut), with a high-pass riser for slams and echo outs."""
    info: dict = {"echo_beats": 0.75, "feedback": LANE_DEFAULTS["echo_feedback"], "throw": None, "roll": None,
                  "fx": None}
    A["volume"] = [(-4.0, 1.0, "hold"), (0.0, 0.0, "hold")]
    room = 4.0 * place.a0  # beats of A before the cut, the roll's source must lie in the file
    if tech == "brake":
        n = 4.0 if bars >= 4 else 2.0
        info["fx"] = {"kind": "brake", "beat": -n, "beats": n}
        S.add("verb", "a", -n, -0.5, 0.5, 0.45)
        return info
    if tech == "spinback":
        info["fx"] = {"kind": "spinback", "beat": -2.0, "beats": 2.0}
        A["filter"] = [(-8.0, 0.0, "ease"), (-2.0, 0.25, "hold")]
        S.add("verb", "a", -4.0, -1.5, 0.0, 0.45)
        S.add("echo", "a", -3.0, -2.0, -1.0, 0.45)
        info["feedback"] = 0.6
        return info
    if into_drop or tech == "drop swap":
        A["filter"] = [(-8.0, 0.0, "ease"), (0.0, 0.35, "hold")]
        if room >= 8:
            a_deck["loops"] = _roll(0.0, 1)
            info["roll"] = (-4.0, 1)
            A["eq_low"] = [(-2.0, 0.0, "linear"), (-1.0, _KILL, "hold")]
        S.add("verb", "a", -8.0, -0.25, 0.0, 0.6)
        return info
    if tech in ("slam", "echo out"):
        riser = 4.0 * max(2, min(bars, 8))
        A["filter"] = [(-riser, 0.0, "ease"), (0.0, 0.45 if tech == "slam" else 0.4, "hold")]
    info.update(_throw(S, 0.0))
    rb = 4 if tech == "slam" else 1 if tech == "quick cut" else 0
    if rb and room >= 4.0 * rb + 4:
        a_deck["loops"] = _roll(0.0, rb)
        info["roll"] = (-4.0 * rb, rb)
        A["eq_low"] = [(-2.0, 0.0, "linear"), (-1.0, _KILL, "hold")]
    return info


def _mid_sends(m: _Mix, tech: str, place: _Place, a_deck: dict, S: _Sends, clash_at: float | None,
               window: tuple[float, float]) -> list[float]:
    """Throws and washes inside the transition (after each technique's own exit FX): an echo accent on A's last beat
    before the bass swap, a throw on A's vocal where the vocals would clash and on its sung phrase ends, and reverb
    washes into the drops at breakdown exits on either deck. Returns the master beats of the vocal throws."""
    E = 4.0 * place.overlap
    throws = []
    if place.overlap and place.swap is not None and tech not in ("loop roll", "filter sweep"):
        sw = 4.0 * place.swap
        S.add("echo", "a", sw - 2, sw - 1, sw, _SWAP_ECHO)
    if clash_at is not None and S.add("echo", "a", clash_at - 3, clash_at - 1, clash_at, _THROW_ECHO):
        throws.append(clash_at)
    if place.overlap:
        for p in [p for p in _vocal_ends(m, a_deck, place) if 4.0 <= p <= E - 4.0][-3:]:
            if len(throws) < 3 and S.add("echo", "a", p - 2, p - 1, p, _THROW_ECHO):
                throws.append(p)
    lo, hi = window
    for be in m.a_breaks:
        beat = 4.0 * (be - place.a0)
        if lo + 8 <= beat <= min(hi, E):
            S.add("verb", "a", beat - 8, beat - 0.5, beat, 0.5)
    for be in m.b_breaks:
        beat = 4.0 * (be - place.b0 + place.hold)
        if 8 <= beat <= hi:
            S.add("verb", "b", beat - 8, beat - 0.5, beat, 0.45)
    return throws


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


_ROLL_WORDS = {4: "8 → 4 → 2 → 1 → ½ beat", 2: "4 → 2 → 1 → ½ beat", 1: "2 → 1 → ½ → ¼ beat"}


def _headline(m: _Mix, tech: str, bars: int, place: _Place, info: dict) -> str:
    ga, gb = m.ga, m.gb
    a0, b0, L = place.a0, place.b0, place.overlap
    t0, tb, te = _mmss(ga, a0), _mmss(gb, b0), _mmss(ga, a0 + L)
    swap = f"swap the bass on the 1 of bar {place.swap + 1} (A {_mmss(ga, a0 + place.swap)})" \
        if place.swap is not None else None
    roll = info.get("roll")
    rolled = f"a {roll[1]}-bar loop roll ({_ROLL_WORDS[roll[1]]})" if roll else None
    if tech == "long blend":
        what = f"{L}-bar long blend: B in at A {t0} (bar {a0 + 1}, on the phrase) from B {tb}, " \
               f"{swap or 'B brings its lows in as A leaves'}, A out by {te}"
    elif tech in ("bass swap", "phrase blend"):
        what = f"{L}-bar {tech}: B in at A {t0} (bar {a0 + 1}) from B {tb}, " + \
               (f"{swap}, A out by {te}" if swap else f"A's bass carries the blend and is cut on the 1 at {te}")
    elif tech == "filter sweep":
        what = f"{L}-bar filter sweep: B opens up from a closed low-pass under A from A {t0} (bar {a0 + 1}) while " \
               f"A rides a high-pass sweep into a reverb wash out by {te}" + (f"; {swap}" if swap else "")
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
        what = f"{L}-bar loop roll: B in at A {t0} (bar {a0 + 1}) from B {tb}; {looped}then A rolls " \
               f"{_ROLL_WORDS[roll[1]] if roll else '1 bar → 2 beats → 1 → ½'} and is cut on the 1 as B's bass drops in"
    elif tech in ("slam", "spinback"):
        into = "its drop" if b0 in gb.drops else "its main section" if b0 == gb.intro[1] else "its cue"
        if tech == "spinback":
            fx = "A is spun back on its last 2 beats into a big reverb wash"
        elif b0 in gb.drops:
            fx = "a clean cut after a short filter riser" + (f" and {rolled}" if rolled else "")
        else:
            fx = (f"{rolled} under " if rolled else "") + \
                 f"a high-pass riser over its last {max(2, min(bars, 8))} bars and an echo throw"
        what = f"{tech}: {fx}, and on the 1 at A {t0} (bar {a0 + 1}) B slams in at {into} (B {tb})"
    elif tech == "brake":
        n = int(info["fx"]["beats"]) if info.get("fx") else 4
        what = f"brake: A powers down like a turntable switched off over its last {n} beats into the 1 at A {t0} " \
               f"(bar {a0 + 1}); B starts on that 1 from B {tb}"
    elif tech == "drop swap":
        what = f"drop swap: let A's build run" + (" into a 1-bar stutter" if roll else "") + \
               f" and on the 1 where A would drop (A {t0}, bar {a0 + 1}) cut into B's drop at {tb}, a reverb wash " \
               f"on A's last bars"
    else:
        what = f"quick cut on the 1 at A {t0} (bar {a0 + 1}) into B from {tb}" + \
               (" after a 1-bar stutter" if roll else "")
    edges = ""
    if m.tr is not None and m.tr.get("notes"):
        first = str(m.tr["notes"][0])
        if " → " in first:
            edges = first.split(" → ", 1)[0] + " → "
    text = edges + what + "."
    return text[0].upper() + text[1:]


def _eq_note(tech: str, place: _Place) -> str | None:
    L = place.overlap
    if tech == "long blend":
        return f"B comes in with its lows killed behind a gentle high-pass, its highs and mids opening over the " \
               f"first {L // 2} bars; A's mids and highs ease down over the second half under a high-pass rising " \
               f"from bar {L // 4 + 1}, and its last bar flies off into an echo throw and a reverb wash."
    if tech in ("bass swap", "phrase blend"):
        return f"B's highs and mids open over the first {max(1, L // 2)} bars with its lows killed; A's mids and " \
               f"highs ease down over the second half under a slowly rising high-pass; A's last beat before the " \
               f"swap is thrown into the echo, and its last bar into an echo and a reverb wash."
    if tech == "filter sweep":
        return "Classic filter crossfade: A's high-pass climbs over the whole blend into a reverb wash and an echo " \
               "at the top, B's low-pass opens over the first half, the bass swaps on the 1."
    if tech == "echo out" and L:
        return "A keeps its fader up under a slowly rising high-pass; a dotted-8th echo (feedback 0.7) catches " \
               "its last 2 beats and rings over B for a few bars after the cut."
    if tech == "loop roll":
        return "B stays bass-free until the roll ends; during the roll A's lows go while a high-pass and a reverb " \
               "wash rise, and a dotted-8th echo catches its last half beat."
    if tech == "brake":
        return "The brake is a varispeed: A's pitch drops with its speed like a turntable powering down, smeared " \
               "into a short reverb."
    if tech == "spinback":
        return "A whips backwards and winds down into a reverb wash; B lands dry on the 1."
    return None


def _fx(raw: object, what: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > _MAX_FX:
        raise ValueError(f"{what}.fx must be a list of at most {_MAX_FX} effects")
    out = []
    for f in raw:
        if not isinstance(f, dict) or f.get("kind") not in FX_KINDS:
            raise ValueError(f"{what}.fx: kind must be one of {FX_KINDS}")
        beat, beats = _req_num(f, "beat", what), _req_num(f, "beats", what)
        out.append({"kind": f["kind"], "beat": beat, "beats": min(FX_BEATS_RANGE[1], max(FX_BEATS_RANGE[0], beats))})
    return sorted(out, key=lambda f: f["beat"])


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
) -> dict:
    """MixPlan (types.ts) for A → B: the engine's (or the requested) technique/blend length as beat-locked lane
    automation, loops, deck FX, tempo moves and a preview window. Works without analyses (rough plan from tag BPMs)."""
    if technique is not None and technique not in TECHNIQUES:
        raise ValueError(f"unknown technique {technique!r}")
    if bars is not None and bars not in BARS:
        raise ValueError(f"bars must be one of {BARS}")
    m = _mix(a, a_an, b, b_an, transition)
    eng = _engine(m)
    e_tech, e_place = eng
    tech = technique or _auto_technique(m, e_tech, e_place)
    notes_extra: list[str] = []

    big_gap = abs(m.b_bpm / m.a_bpm - 1) > _MAX_BLEND_GAP
    bars_given = bars is not None
    into_drop = False
    stretch = None
    if tech in _CUTS or (tech == "echo out" and bars is None and (e_place.overlap == 0 or big_gap)):
        place = _cut_place(m, tech, eng)
        bars = bars or 4
    elif tech == "loop roll":
        bars = bars or _default_bars(m, tech, e_place)
        if e_place.overlap and e_tech not in _CUTS:
            place = _Place(e_place.a0, e_place.b0, bars, None)
        else:
            cut = _cut_place(m, "quick cut", eng)
            place = _Place(cut.a0, cut.b0, bars, None)
    else:
        ext, note = (e_place, None) if bars_given or not e_place.overlap or e_tech in _CUTS \
            else _extend(m, tech, e_place, fixed=False)
        if note:
            place, bars = ext, ext.overlap
            notes_extra.append(note)
            if tech in ("bass swap", "phrase blend"):
                tech = "long blend"
        else:
            bars = bars or _default_bars(m, tech, e_place)
            place = _blend_place(m, bars, eng)
            place, note = _clear_a_drop(m, place)
            notes_extra += [note] if note else []
            place, note = _land_on_drop(m, place, fixed_len=bars_given)
            if note:
                into_drop = True
                notes_extra.append(note)
                if tech == "bass swap":
                    tech = "long blend" if place.overlap >= 16 else "phrase blend"
                bars = place.overlap
        place, note = _auto_hold(m, tech, place)
        notes_extra += [note] if note else []
        stretch = _stretch(m, place)
        if stretch is not None and not any("loop to stretch" in n for n in notes_extra):
            size = int(stretch["length_beats"] // 4)
            notes_extra.append(f"A runs out before the blend ends, so its last {size} clean bars loop from bar "
                               f"{barno(stretch['start_beat'])} of the blend until it is out.")
        elif stretch is None and place.a0 + place.overlap > m.ga.end and m.real_a:
            notes_extra.append(f"A runs out {place.a0 + place.overlap - m.ga.end} bars before the blend ends: "
                               "try a loop roll or a shorter blend.")
    L = place.overlap
    E = 4.0 * L
    ga, gb = m.ga, m.gb

    a_deck = {
        "track_id": a.id, "beat0_s": round(ga.fd + place.a0 * ga.bar_s, 4), "native_bpm": m.a_bpm,
        "play_from_beat": 0.0, "play_to_beat": E, "echo_beats": 0.75, "loops": [stretch] if stretch else [],
        "fx": [], "lanes": {},
    }  # fmt: skip
    b_beat0 = gb.fd + (place.b0 - place.hold) * gb.bar_s
    b_deck = {
        "track_id": b.id, "beat0_s": round(b_beat0, 4), "native_bpm": m.b_bpm,
        "play_from_beat": 0.0 if place.hold else max(0.0, round(-b_beat0 * m.b_bpm / 60.0, 4)), "play_to_beat": 0.0,
        "echo_beats": 0.75, "fx": [], "lanes": {},
        "loops": [{"start_beat": 0.0, "end_beat": 4.0 * place.hold, "source_beat": 4.0 * place.hold,
                   "length_beats": 4.0 * place.hold_size}] if place.hold else [],
    }  # fmt: skip
    a_deck["play_from_beat"] = round(min(-1.0, -a_deck["beat0_s"] * m.a_bpm / 60.0), 4)
    b_end_s = m.b_dur if m.b_dur > 0 else gb.fd + gb.n * gb.bar_s
    b_deck["play_to_beat"] = round(max(E + 8.0, (b_end_s - b_beat0) * m.b_bpm / 60.0), 4)

    A, B = _lanes(), _lanes()
    S = _Sends()
    if tech in _CUTS or (tech == "echo out" and L == 0):
        info = _cut_lanes(m, tech, bars, A, a_deck, S, place, place.b0 in gb.drops)
    elif tech == "loop roll":
        info = _loop_roll(m, place, A, B, a_deck, S)
    else:
        info = _blend_lanes(tech, L, place.swap, A, B, S, into_drop)
    clash = _clash_bars(m, a_deck, place) if tech not in ("filter sweep",) else []
    clash_at = _cut_mids(A, clash[0], E) if clash and L else None
    a_deck["echo_beats"] = info["echo_beats"]
    if info["feedback"] != LANE_DEFAULTS["echo_feedback"]:
        A["echo_feedback"] = [(0.0, info["feedback"], "hold")]
    if info.get("fx"):
        a_deck["fx"] = [info["fx"]]
        a_deck["play_to_beat"] = max(a_deck["play_to_beat"], info["fx"]["beat"] + info["fx"]["beats"])

    tempo = _tempo(m, E, cut=L == 0)

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

    throws = _mid_sends(m, tech, place, a_deck, S, clash_at, (ws, we))
    for deck, lanes in (("a", A), ("b", B)):
        lanes["echo_send"] = S.lane(S.echo[deck])
        lanes["reverb_send"] = S.lane(S.verb[deck])
    lanes_a = {lane: _kf(A[lane], ws, lane) for lane in LANES}
    lanes_b = {lane: _kf(B[lane], ws, lane) for lane in LANES}
    a_deck["lanes"], b_deck["lanes"] = lanes_a, lanes_b
    tempo_kf = _kf(tempo.keyframes, ws, "volume")
    for k in tempo_kf:
        k["value"] = round(k["value"], 3)

    markers = [{"beat": 0.0, "label": "B in" if L else "cut"}]
    if L and tech != "loop roll" and place.swap is not None:
        markers.append({"beat": 4.0 * place.swap, "label": "bass swap"})
    if stretch is not None:
        markers.append({"beat": stretch["start_beat"], "label": "A loop"})
    if place.hold:
        markers += [{"beat": 0.0, "label": "B loop"}, {"beat": 4.0 * place.hold, "label": "B plays on"}]
    if tech == "loop roll":
        if info.get("loop"):
            markers.append({"beat": info["loop"]["start_beat"], "label": "loop"})
        markers.append({"beat": E, "label": "bass swap"})
    if info.get("roll"):
        markers.append({"beat": info["roll"][0], "label": "loop roll"})
    if info.get("fx"):
        markers.append({"beat": info["fx"]["beat"], "label": info["fx"]["kind"]})
    if info["throw"] is not None:
        markers.append({"beat": info["throw"], "label": "echo throw"})
    markers += [{"beat": t - 1.0, "label": "echo throw"} for t in throws]
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
        notes.append(f"Vocals on both decks in {where} of the blend — A's mids are ducked to "
                     f"{_VOCAL_CUT:.0f} dB from bar {k0 + 1} and its vocal is thrown into the echo so they don't clash.")
    if throws and not clash:
        notes.append(f"A's sung phrase{'s' if len(throws) > 1 else ''} ending in bar"
                     f"{'s' if len(throws) > 1 else ''} {', '.join(str(int(t // 4)) for t in throws)} of the blend "
                     f"{'are' if len(throws) > 1 else 'is'} thrown into the echo so the vocal trails off over B.")
    notes.append(tempo.note)
    notes.extend(notes_extra)
    if not (m.real_a and m.real_b):
        missing = " and ".join(n for n, ok in (("A", m.real_a), ("B", m.real_b)) if not ok)
        notes.append(f"Rough plan from tag BPMs: {missing} {'is' if ' and ' not in missing else 'are'} not "
                     "analysed yet, so beats and phrases may not line up.")

    plan = {
        "version": PLAN_VERSION, "folder": folder, "from_id": a.id, "to_id": b.id, "technique": tech,
        "bars": bars, "key_lock": True, "window": {"start_beat": ws, "end_beat": we}, "tempo": tempo_kf,
        "a": a_deck, "b": b_deck, "markers": sorted(markers, key=lambda mk: mk["beat"]), "notes": notes,
        "edited": False,
    }  # fmt: skip
    return validate(plan)


def barno(beat: float) -> int:
    """1-based bar of the blend a master beat falls in."""
    return int(beat // 4) + 1
