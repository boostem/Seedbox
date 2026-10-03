"""Mix plans (APP_SPEC.md v4): the automation script of one A → B transition, which mixrender turns into audio.

Everything lives on one MASTER timeline in beats. Beat 0 is where B comes in, on A's phrase grid (the placement of
transitions.suggest). Each deck maps master beats to its own file time through its beat grid (deck_time, loops
honoured) and is time-stretched with key lock so its beats land on the master clock, whose BPM follows `tempo`.
Lanes (fader, 3-band isolator EQ, bipolar filter, pre-fader echo/reverb sends) are keyframed in master beats, and
lane_value is the one interpolation the renderer, the UI and the tests share.

A master bar is 4 master beats: A bar a0 + k and B bar b0 + k sound together in master bar k (a half/double-time B
is put on A's grid first, as in transitions, so its native_bpm is the analysis BPM x 2 or x 0.5). `bars` is the
blend length; for the cuts (slam, quick cut, drop swap, brake, spinback, an echo out without overlap) it is the
length of A's lead-in effect (filter riser) before the cut on the 1.

Generated plans never loop a deck: each track plays straight through, and the blend's placement and length come
from the music itself (A's outro, B's intro, phrases, drops). Where the vocals would clash A's mids are ducked and
its last word goes into the echo. Loop rolls (8 -> 4 -> 2 -> 1 -> 1/2 beat) only happen when asked for: the "loop
roll" technique and the "roll" style.

Every transition has a STYLE. The default is the phrase mix: B eases in and A eases out over whole phrases, the way
most DJ mixes are built, and it can repeat as often as it suits the songs.

Two planners share this module (MODE, env SEEDBOX_MIX_MODE). "chill", the default, shapes every transition with the
EQ, the filter and the vocal stems alone (see "chill planner" below). "fx" is the earlier planner, kept intact but
switched off: technical styles (vocal throw, roll into the drop, swell, wash, dub echo, slam, brake, spinback,
double drop, ...) scored on how well they fit the pair, at most three of one in a row (plan_styles), with echo and
reverb sends.
"""

from __future__ import annotations

import copy
import math
import os
from dataclasses import dataclass, field

import numpy as np

from . import mixing, transitions
from .models import Track

PLAN_VERSION = 4
MODE = "fx" if os.environ.get("SEEDBOX_MIX_MODE", "").strip().lower() == "fx" else "chill"

# vocals / instrumental: the deck's vocal stem and the rest of the track (mix minus vocals), 0-1 each; both at 1 is
# the original track. They only act once the track's vocals are split (musicreco.stems), else the track plays whole.
LANES = ("volume", "eq_low", "eq_mid", "eq_high", "filter", "vocals", "instrumental", "echo_send", "echo_feedback",
         "reverb_send")
STEM_LANES = ("vocals", "instrumental")
FX_LANES = ("echo_send", "echo_feedback", "reverb_send")  # the FX planner's sends (never written by the chill one)
LANE_RANGES: dict[str, tuple[float, float]] = {
    "volume": (0.0, 1.0), "eq_low": (-30.0, 6.0), "eq_mid": (-30.0, 6.0), "eq_high": (-30.0, 6.0),
    "filter": (-1.0, 1.0), "vocals": (0.0, 1.0), "instrumental": (0.0, 1.0), "echo_send": (0.0, 1.0),
    "echo_feedback": (0.0, 0.95), "reverb_send": (0.0, 1.0),
}  # fmt: skip
LANE_DEFAULTS: dict[str, float] = {
    "volume": 1.0, "eq_low": 0.0, "eq_mid": 0.0, "eq_high": 0.0, "filter": 0.0, "vocals": 1.0, "instrumental": 1.0,
    "echo_send": 0.0, "echo_feedback": 0.55, "reverb_send": 0.0,
}  # fmt: skip
CURVES = ("linear", "hold", "ease")
TECHNIQUES = (*transitions.TECHNIQUES, "filter sweep", "loop roll", "slam", "brake", "spinback", "double drop")
BARS = (4, 8, 16, 32)
TEMPO_RANGE = (40.0, 300.0)
ECHO_BEATS_RANGE = (0.125, 4.0)
FX_KINDS = ("brake", "spinback")
FX_BEATS_RANGE = (0.25, 16.0)

_CUTS = ("drop swap", "quick cut", "slam", "brake", "spinback")
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
_LOOP_VOCAL_MAX = 0.4
_MAX_PREVIEW_S = 92.0
_MAX_WINDOW_BEATS = 512
_MAX_KEYFRAMES = 4096
_MAX_LOOPS = 64
_MAX_FX = 8
_MAX_ABS_BEAT = 100_000.0
_MIN_LOOP_BEATS = 1 / 16
_VOCAL_CUT = -18.0  # dB on A's mids while vocals overlap
_THROW_ECHO = 0.55  # echo send of the clash throw on A's last word before its mids duck (kept short: 0.5 feedback)
_ECHO_SEND = (0.75, 1.0)  # echo throws that have to be heard over B (see _echo_shape)
_BASS_LOW = transitions._BASS_ON  # mean low_per_bar at/above this = real kick/bass weight

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
    fit = [{"style": f["style"], "fit": min(1.0, max(0.0, float(f["fit"]))), "why": str(f.get("why") or "")[:160]}
           for f in (p.get("style_fit") if isinstance(p.get("style_fit"), list) else [])
           if isinstance(f, dict) and f.get("style") in STYLES and _finite(f.get("fit"))][: len(STYLES)]
    return {
        "version": PLAN_VERSION, "folder": p.get("folder", ""), "from_id": p["from_id"], "to_id": p["to_id"],
        "technique": p["technique"], "bars": min(BARS, key=lambda n: abs(n - bars)),
        "key_lock": bool(p.get("key_lock", True)), "window": {"start_beat": ws, "end_beat": we},
        "tempo": tempo, "a": a, "b": b, "markers": markers, "notes": notes, "edited": bool(p.get("edited", False)),
        "style": p["style"] if p.get("style") in STYLES
        else p["technique"] if p["technique"] in STYLES else DEFAULT_STYLE,
        "style_fit": fit,
        "swap_kind": p["swap_kind"] if p.get("swap_kind") in SWAP_KINDS else None,
        "vocal_stems": {d: bool((p.get("vocal_stems") or {}).get(d)) for d in ("a", "b")}
        if isinstance(p.get("vocal_stems"), dict) else {"a": False, "b": False},
    }  # fmt: skip


# --- planning: inputs ----------------------------------------------------------------------------------------------


@dataclass
class _Place:
    a0: int  # A bar sounding at master bar 0
    b0: int  # B's cue: the B bar it starts from at master bar 0
    overlap: int  # bars both decks play (0 = cut on the 1 at master beat 0)
    swap: int | None  # 0-based overlap bar where B's low end replaces A's; None = B's lows come in as A leaves


def _b_bar(p: _Place, k: int) -> int:
    """B bar sounding at master bar k."""
    return p.b0 + k


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
    stems_a: bool = False  # A's vocal stem is split: vocal_per_bar is the stem's own activity, the vocals lane acts
    stems_b: bool = False


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
        b_dur=b_dur, stems_a=real_a and (a_an or {}).get("vocal_source") == "stems",
        stems_b=real_b and (b_an or {}).get("vocal_source") == "stems",
    )  # fmt: skip


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
            f"Blend lengthened to {need} bars so A leaves right on B's drop (B {_mmss(gb, d)}) instead of B's quiet part."
    b0 = d - place.overlap
    return _Place(place.a0, b0, place.overlap, None), \
        f"B is cued {b0 - place.b0} bars later (B {_mmss(gb, b0)}) so its quiet part runs under A and A leaves " \
        f"right on B's drop (B {_mmss(gb, d)})."


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


def _roll_into(a_deck: dict, at: float, bars: int = 1, tail: float = 1.0) -> float:
    """Add a loop roll ending on master beat `at` to deck A, cutting into any loop it overlaps (the roll starts from
    where that loop is). Returns the roll's first beat."""
    r = at - 4.0 * bars
    base = r
    kept = []
    for lp in a_deck["loops"]:
        if lp["end_beat"] <= r:
            kept.append(lp)
        elif lp["start_beat"] < r:
            base = lp["source_beat"] + math.fmod(r - lp["start_beat"], lp["length_beats"])
            kept.append({**lp, "end_beat": r})
    roll = _roll(at, bars, base)
    roll[-1]["end_beat"] = at + tail
    a_deck["loops"] = kept + roll
    return r


def _double_drop_place(m: _Mix) -> _Place | None:
    """A's last drop and B's first drop on the same 1: B in under A's 8-bar build, both drop together on master
    bar 8, the bass goes to B on bar 12, A out by bar 16. Needs both analysed, an in-key pair at almost the same
    tempo, a late drop in A with 8 bars of it to play, and no vocals on both decks through the double drop."""
    ga, gb = m.ga, m.gb
    if not (m.real_a and m.real_b) or m.move not in _GOOD_KEYS or abs(m.b_bpm / m.a_bpm - 1) > _SMALL_GAP:
        return None
    a_drops = [d for d in ga.drops if d >= max(8, ga.end * 0.45) and d + 8 <= ga.end]
    b_drops = [d for d in gb.drops if 8 <= d and d + 8 <= gb.n]
    if not a_drops or not b_drops:
        return None
    da, db = a_drops[-1], b_drops[0]
    if any(ga.vocal[da + k] >= transitions._VOCAL_ON and gb.vocal[db + k] >= transitions._VOCAL_ON for k in range(8)):
        return None
    return _Place(da - 8, db - 8, 16, 12)


def _double_drop_lanes(A: dict, B: dict) -> dict:
    """B fades in under A's build with its lows killed; A's bass drops out for the beat before the drop under a
    rising high-pass; both drop together, B's bass held back and its mids a touch down so the drops stack without
    mud; the bass goes to B four bars in and A eases out over the last phrase under a high-pass."""
    B["volume"] = [(0.0, 0.0, "ease"), (24.0, 0.8, "linear"), (32.0, 0.85, "hold"), (48.0, 1.0, "hold")]
    B["eq_low"] = [(0.0, _KILL, "hold"), (48.0, _KILL, "linear"), (49.0, 0.0, "hold")]
    B["eq_mid"] = [(0.0, -12.0, "ease"), (32.0, -3.0, "hold"), (48.0, 0.0, "hold")]
    B["eq_high"] = [(0.0, -10.0, "ease"), (30.0, 0.0, "hold")]
    B["filter"] = [(0.0, 0.25, "ease"), (30.0, 0.0, "hold")]
    A["eq_low"] = [(30.0, 0.0, "linear"), (31.0, _KILL, "hold"), (32.0, 0.0, "hold"), (48.0, 0.0, "linear"),
                   (49.0, _KILL, "hold")]
    A["eq_mid"] = [(32.0, 0.0, "ease"), (36.0, -3.0, "hold"), (48.0, -3.0, "ease"), (64.0, -12.0, "hold")]
    A["eq_high"] = [(48.0, 0.0, "ease"), (64.0, -12.0, "hold")]
    A["filter"] = [(24.0, 0.0, "ease"), (31.5, 0.2, "hold"), (32.0, 0.0, "hold"), (52.0, 0.0, "ease"),
                   (64.0, 0.3, "hold")]
    A["volume"] = [(56.0, 1.0, "ease"), (64.0, 0.0, "hold")]
    return {"echo_beats": 0.75, "feedback": LANE_DEFAULTS["echo_feedback"], "throw": None, "roll": None}


# --- planning: styles -----------------------------------------------------------------------------------------------
#
# A transition's STYLE is what a listener hears it as. Most DJ mixing is phrase mixing: B comes in on the phrase,
# slow ease in, bass swap, slow ease out; that's the default and it can repeat as often as it fits. The technical
# moves are scored against the two songs (a vocal ending over B's instrumental invites an echo throw, pads at B's
# start invite a reverb swell, a big tempo jump into B's drop invites a spinback...) and only win where they fit
# better; a flashy style never runs more than three times in a row, and a style used lately loses a little so close
# calls vary. The loop-based styles (roll, loop roll) are never picked on their own: only when asked for.

DEFAULT_STYLE = "phrase mix"
BLEND_STYLES = ("phrase mix", "vocal throw", "echo exit", "roll", "swell in", "wash exit", "filter wash", "dub echo")
STYLES = (*BLEND_STYLES, "filter sweep", "loop roll", "double drop", "echo out", "slam", "quick cut", "drop swap",
          "brake", "spinback", "acapella", "filter fade")
_BLENDS = ("long blend", "phrase blend", "bass swap")  # the phrase-mixing family (its style is its flavour)
_MANUAL_ONLY = ("roll", "loop roll")  # loop-based: only when asked for
_MAX_STREAK = 3
_RECENT_PENALTY = 0.04  # per use in the last four transitions (close calls go to something fresh)
_FAMILY_PENALTY = 0.03  # per use of a style that sounds alike (an echo out after an echo exit...)
_RECENT_BIG = 0.25  # a double drop / spinback / brake used lately: save it
_FAMILY = {
    "echo out": "echo", "echo exit": "echo", "dub echo": "echo", "vocal throw": "echo",
    "wash exit": "wash", "swell in": "wash", "filter wash": "wash", "filter sweep": "wash",
    "roll": "roll", "loop roll": "roll", "slam": "roll", "brake": "deck", "spinback": "deck",
    "quick cut": "cut", "drop swap": "cut",
}  # fmt: skip


@dataclass
class _Choice:
    style: str
    score: float  # how well the style fits this pair, 0-1 (0 = not possible here)
    why: str = ""  # the reason, in a few words (shown next to the style in the editor)


def _choose(choices: list[_Choice], recent: list[str]) -> _Choice:
    """The best-fitting choice given the styles of the transitions before it (most recent last)."""
    last = recent[-4:]

    def adjusted(c: _Choice) -> float:
        if c.style == DEFAULT_STYLE:
            return c.score
        big = _RECENT_BIG if c.style in ("double drop", "spinback", "brake") and c.style in last else 0.0
        fam = _FAMILY.get(c.style)
        alike = sum(1 for r in last if r != c.style and fam is not None and _FAMILY.get(r) == fam)
        return c.score - _RECENT_PENALTY * last.count(c.style) - _FAMILY_PENALTY * alike - big

    for c in sorted(choices, key=lambda c: (adjusted(c), c.style == DEFAULT_STYLE), reverse=True):
        if c.score <= 0 or c.style in _MANUAL_ONLY:
            continue
        if c.style != DEFAULT_STYLE and len(recent) >= _MAX_STREAK and all(r == c.style for r in recent[-_MAX_STREAK:]):
            continue
        return c
    return next((c for c in choices if c.style == DEFAULT_STYLE), _Choice(DEFAULT_STYLE, 0.5))


def _base_technique(m: _Mix, e_tech: str, e: _Place) -> str:
    """The structural call from the music alone: "cut" when there's nothing to blend (or the tempos are too far
    apart), a drop swap when the engine lines drops up, a filter sweep over awkward keys or a vocal outro, a blend
    into an echo out when A stops cold, else the engine's blend (long blend when it's long and in key)."""
    gap = abs(m.b_bpm / m.a_bpm - 1)
    if e.overlap and gap > _MAX_BLEND_GAP:
        return "cut"
    if e.overlap == 0:
        return "drop swap" if e_tech == "drop swap" else "cut"
    if not (m.real_a and m.real_b):
        return e_tech
    ga, gb = m.ga, m.gb
    good_key = m.move in _GOOD_KEYS
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


def _body_energy(g: _Grid) -> float:
    lo, hi = g.intro[1], min(g.end, g.intro[1] + 32)
    return max(0.05, _mean(g.energy, lo, hi) if hi > lo else _mean(g.energy, 0, g.n))


def _blend_kind(m: _Mix, p: _Place) -> str:
    """The engine's name for a blend at `p`: long blend (drums-only outro over drums-only intro, 16+ bars), bass swap
    (both decks carry bass), else phrase blend."""
    ga, gb = m.ga, m.gb
    if p.overlap >= 16 and ga.outro_drums and p.a0 >= ga.outro[0] and gb.intro_drums \
            and p.b0 + p.overlap <= gb.intro[1]:
        return "long blend"
    if _mean(ga.low, p.a0, p.a0 + p.overlap) >= _BASS_LOW and _mean(gb.low, p.b0, p.b0 + p.overlap) >= _BASS_LOW:
        return "bass swap"
    return "phrase blend"


def _exit_choices(m: _Mix, eng: tuple[str, _Place], base: str) -> list[_Choice]:
    """How well each hard exit suits a pair that can't be blended: an echo out when A's last bars are sung (the
    word trails off), a slam (roll + riser) when B has a drop to slam into after a full-power A, a brake when B
    starts quiet after a full-power A (powering down into calm), a spinback for a big tempo jump into B's drop, a
    quick cut on an unsung bar line, the engine's drop swap when it lined the drops up."""
    ga, gb = m.ga, m.gb
    out_bar = _cut_place(m, "quick cut", eng).a0
    tail = (max(0, out_bar - 2), out_bar)
    a_hot = _mean(ga.energy, *tail) >= 0.75
    a_sung = _mean(ga.vocal, *tail) >= 0.4
    into_drop = _cut_place(m, "slam", eng).b0 in gb.drops
    cue = transitions._first_cue(gb)
    b_quiet = _mean(gb.energy, cue, cue + 4) < 0.55 * _body_energy(gb)
    jump = abs(m.b_bpm / m.a_bpm - 1) > 0.08
    return [
        _Choice("echo out", *((0.72, "A's last bars are sung: the last word trails off") if a_sung
                              else (0.5, "a safe exit over any ending"))),
        _Choice("slam", *((0.7, "A is at full power and B has a drop to slam into") if into_drop and a_hot
                          else (0.58, "B has a drop to slam into") if into_drop else (0.42, "no drop in B to slam into"))),
        _Choice("quick cut", *((0.45, "A is mid-vocal: a dry cut would chop the word") if a_sung
                               else (0.55, "a clean bar line to cut on"))),
        _Choice("brake", *((0.68, "full-power A into a quiet B: powering down sets it up") if b_quiet and a_hot
                           else (0.25, "B doesn't start quiet enough for a power-down"))),
        _Choice("spinback", *((0.72, "a big tempo jump into B's drop: the spin hides it") if jump and into_drop
                              else (0.45, "B has a drop to spin into") if into_drop and a_hot
                              else (0.18, "nothing big to spin into"))),
        _Choice("drop swap", *((0.72, "the drops line up: A's build cuts into B's drop") if base == "drop swap"
                               else (0.0, "the drops don't line up"))),
    ]


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
                      f"({gap * 100:+.1f}%): the cut hides the tempo change.")
    if abs(gap) <= _SMALL_GAP:
        post = _ramp_bars(gap)
        what = "the blend" if not cut else "the cut"
        return _Tempo([(0.0, a, "hold"), (end, a, "ease"), (end + 4 * post, b, "hold")], None, end + 4 * post, a,
                      f"{half}B is pitched {-gap / (1 + gap) * 100:+.1f}% to A's {a:.1f} BPM through {what} (key "
                      f"lock on), then the tempo drifts to B's own {b:.1f} BPM over {post} bars once A is out.")
    meet = a * math.sqrt(b / a)
    pre, post = _ramp_bars(meet / a - 1), _ramp_bars(b / meet - 1)
    kf = [(-4.0 * pre, a, "ease"), (0.0, meet, "hold"), (end, meet, "ease"), (end + 4 * post, b, "hold")]
    loud = (" Big tempo change: these ramps will be audible" + ("; an echo out hides it better." if MODE == "fx"
                                                               else ", key lock keeps the pitch.")) \
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


def _echo_beats(bpm: float) -> float:
    """Delay time that reads as a clear echo at this tempo: the dotted 8th (3/4 beat) when its repeats land
    0.28-0.55 s apart, else the 8th for slow songs and the quarter for fast ones."""
    for beats in (0.75, 0.5, 1.0):
        if 0.28 <= beats * 60.0 / bpm <= 0.55:
            return beats
    return 0.5 if bpm < 100 else 1.0


@dataclass
class _Echo:
    send: float  # echo send on the captured beat
    fb: float  # feedback: how many repeats ring
    beats: float  # delay time
    duck: float  # dB B's mids dip while the first repeats ring (0 = none)


def _echo_shape(m: _Mix, place: _Place, k: float, into_drop: bool = False) -> _Echo:
    """The echo throw for repeats that have to be heard over B from master bar k.

    Level: energy_per_bar is 0-1 over the 18 dB below each track's loud bars and every deck is trimmed to the same
    loudness, so 18 * (eB - eA) is about how many dB louder B comes in than A's last bar (a sung entry counts 6 dB
    more: it fills the band the repeats live in). The louder B, the hotter the send and the deeper B's mids dip for
    the repeats (up to 6 dB; B's kick and bass still land at full).
    Tail: the repeats should be gone (24 dB down) before B's next vocal or drop, so the feedback comes from that gap
    (0.35-0.72: from 2-3 repeats before a vocal to 6-8 into a long quiet entry); into a drop it rings 3 times. When A's
    last beat is sung and B comes in singing, the word echoes twice and gets out of the way (two vocals never sit on
    top of each other)."""
    ga, gb = m.ga, m.gb
    k = max(0.0, k)
    kb = _b_bar(place, int(k))
    ka = place.a0 + int(k) - 1
    beats = _echo_beats(m.a_bpm)
    eb = _mean(gb.energy, kb, kb + 2) if m.real_b else 0.7
    ea = _mean(ga.energy, ka, ka + 1) if m.real_a else 0.7
    b_sung = m.real_b and _mean(gb.vocal, kb, kb + 2) >= transitions._VOCAL_ON
    if b_sung and m.real_a and _mean(ga.vocal, ka, ka + 1) >= 0.4:
        return _Echo(0.85, 0.45, beats, 2.0)
    louder = 18.0 * (eb - ea) + (6.0 if b_sung else 0.0)
    send = min(_ECHO_SEND[1], max(_ECHO_SEND[0], 0.85 + louder / 40.0))
    duck = min(6.0, max(0.0, 2.0 + louder / 2.0))
    if into_drop:
        fb = 0.52
    else:
        nxt = next((j for j in range(kb + 1, min(gb.n, kb + 8))
                    if gb.vocal[j] >= transitions._VOCAL_ON or j in gb.drops), kb + 8) if m.real_b else kb + 4
        n = min(8.0, max(2.0, 4.0 * (nxt - kb) / beats))  # repeats that fit before B's next vocal / drop
        fb = min(0.72, max(0.35, 10 ** (-24.0 / (20.0 * (n - 1.0)))))
    return _Echo(round(send, 2), round(fb, 2), beats, round(duck, 1))


def _duck_mids(B: dict, at: float, duck: float) -> None:
    """Dip B's mids by `duck` dB while the first repeats of an echo throw ending at `at` ring, easing back over the
    next bar and a half."""
    if duck <= 0:
        return
    pts = sorted(B["eq_mid"], key=lambda p: p[0])
    v0 = float(lane_value([{"beat": b, "value": v, "curve": c} for b, v, c in pts], at - 1.0)) if pts else 0.0
    B["eq_mid"] = [p for p in pts if p[0] < at - 1.0] + [
        (at - 1.0, v0, "linear"), (at - 0.25, v0 - duck, "hold"), (at + 2.0, v0 - duck, "ease"), (at + 6.0, v0, "hold")]


def _repeats(fb: float) -> int:
    """Echo repeats heard before the tail is 18 dB down."""
    return max(1, int(18.0 / max(0.5, -20.0 * math.log10(max(0.05, fb)))))


def _echo_words(beats: float) -> str:
    return {0.5: "8th-note", 0.75: "dotted-8th", 1.0: "quarter-note"}.get(beats, f"{beats:g}-beat")


def _throw(S: _Sends, B: dict, at: float, e: _Echo) -> dict:
    """Echo throw ending on the 1 at `at`: A's send is wide open on its last beat only (a ~25 ms ramp in, closed on
    the 1), so the delay repeats exactly that beat (pre-fader: the repeats ring on after A's fader is down) and fades
    out over B, whose mids dip to make room for the first repeats."""
    S.add("echo", "a", at - 1.05, at - 1.0, at, e.send)
    _duck_mids(B, at, e.duck)
    return {"echo_beats": e.beats, "feedback": e.fb, "throw": at - 1.0}


def _blend_lanes(tech: str, length: int, swap: int | None, A: dict, B: dict, S: _Sends, into_drop: bool,
                 echo: _Echo) -> dict:
    """Phrase mixing, spread over the whole overlap (same shape for 4/8/16/32 bars): B's fader eases up over the
    first half while its highs, mids and filter open; the bass swaps on the 1 of the swap bar with a 1-beat
    crossfade; A's mids/highs ease down over the second half under a gentle high-pass and its fader eases out over
    the last three eighths; A leaves clean on the phrase (the style adds any FX). Without a swap (B brings no bass, or
    A rides until B's drop) A's bass carries to the end and the lows change hands on the 1. `echo` = the throw a
    blend into an echo out ends with."""
    E = 4.0 * length
    sw = 4.0 * swap if swap is not None else None
    q = E / 4
    half = sw if sw is not None and sw <= 0.75 * E else E / 2
    info: dict = {"echo_beats": 0.75, "feedback": 0.6, "throw": None}
    B["volume"] = [(0.0, 0.0, "ease"), (max(4.0, half if tech in _BLENDS else q), 1.0, "hold")]
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
    sweep = 0.85 if tech == "filter sweep" else 0.25
    A["filter"] = [(0.0 if tech == "filter sweep" else q, 0.0, "ease"), (E, sweep, "hold")]
    if tech == "echo out":  # A's fader stays up; cut on the 1 under a long echo throw
        A["eq_mid"] = [(out0, 0.0, "ease"), (E, -6.0, "hold")]
        A["eq_high"] = [(out0, 0.0, "ease"), (E, -6.0, "hold")]
        A["volume"] = [(E - 4, 1.0, "hold"), (E, 0.0, "hold")]
        info.update(_throw(S, B, E, echo))
        return info
    fade = max(8.0, 3 * E / 8) if tech in _BLENDS else max(8.0, q)
    if sw is None:  # A rides, bass and full energy, to the 1 where B's lows land (no dip before B's drop)
        A["eq_mid"] = [(out0, 0.0, "ease"), (E, -6.0, "hold")]
        A["eq_high"] = [(out0, 0.0, "ease"), (E, -6.0, "hold")]
        A["filter"] = [(E - 8, 0.0, "ease"), (E, 0.12, "hold")]
        A["volume"] = [(E - 4, 1.0, "hold"), (E, 0.0, "hold")]
    else:
        A["eq_mid"] = [(out0, 0.0, "ease"), (E, -8.0, "hold")]
        A["eq_high"] = [(out0, 0.0, "ease"), (E, -10.0, "hold")]
        A["volume"] = [(E - fade, 1.0, "ease"), (E, 0.0, "hold")]
    if tech == "filter sweep":  # a light wash at the top of the sweep
        S.add("verb", "a", E - 8, E, E + 2, 0.25)
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
    S.add("echo", "a", E - 1, E - 0.25, E + 4, 0.6)
    return {"echo_beats": 0.75, "feedback": 0.65, "throw": E - 1, "roll": (R, rb),
            "loop": loops[0] if blk and len(loops) > len(roll) else None}


def _cut_lanes(tech: str, bars: int, A: dict, B: dict, S: _Sends, into_drop: bool, echo: _Echo) -> dict:
    """Cuts on the 1 at master beat 0, B straight in at full. Brake: A powers down over its last bar. Spinback: A is
    spun back on its last 2 beats into a short wash. Into B's drop: a short filter riser, A's bass out for its last
    beat and a short reverb swell, B's drop dry on the 1. Otherwise an echo throw on A's last beats, over a high-pass
    riser for slams and echo outs (a slam also drops A's bass for its last beat); under an echo out B comes in a
    touch down, its mids dipped, so the first repeats are heard."""
    info: dict = {"echo_beats": 0.75, "feedback": LANE_DEFAULTS["echo_feedback"], "throw": None, "roll": None,
                  "fx": None}
    A["volume"] = [(-4.0, 1.0, "hold"), (0.0, 0.0, "hold")]
    if tech == "brake":
        n = 4.0 if bars >= 4 else 2.0
        info["fx"] = {"kind": "brake", "beat": -n, "beats": n}
        S.add("verb", "a", -n / 2, -0.5, 0.5, 0.15)
        return info
    if tech == "spinback":
        info["fx"] = {"kind": "spinback", "beat": -2.0, "beats": 2.0}
        A["filter"] = [(-8.0, 0.0, "ease"), (-2.0, 0.25, "hold")]
        S.add("verb", "a", -3.0, -1.5, 0.0, 0.3)
        S.add("echo", "a", -3.0, -2.0, -1.0, 0.3)
        info["feedback"] = 0.6
        return info
    if into_drop or tech == "drop swap":
        A["filter"] = [(-8.0, 0.0, "ease"), (0.0, 0.35, "hold")]
        A["eq_low"] = [(-2.0, 0.0, "linear"), (-1.0, _KILL, "hold")]
        S.add("verb", "a", -2.0, -0.25, 0.0, 0.3)  # a short swell into the drop, which lands dry
        return info
    if tech in ("slam", "echo out"):
        riser = 4.0 * max(2, min(bars, 8))
        A["filter"] = [(-riser, 0.0, "ease"), (0.0, 0.45 if tech == "slam" else 0.4, "hold")]
    if tech == "slam":
        A["eq_low"] = [(-2.0, 0.0, "linear"), (-1.0, _KILL, "hold")]
    if tech == "echo out":
        B["volume"] = [(0.0, 0.88, "ease"), (4.0, 1.0, "hold")]
        info.update(_throw(S, B, 0.0, echo))
    else:  # slam / quick cut: a shorter echo, no dip (the cut itself is the move)
        info.update(_throw(S, B, 0.0, _Echo(round(echo.send * 0.85, 2), 0.5, echo.beats, 0.0)))
    return info


def _mid_sends(m: _Mix, tech: str, place: _Place, a_deck: dict, S: _Sends, clash_at: float | None) -> list[float]:
    """The vocal-clash throw: A's last word before the vocals would clash (A's mids duck there) goes into the echo.
    Returns its master beat."""
    if not place.overlap or tech in ("echo out", "loop roll") or clash_at is None:
        return []
    return [clash_at] if S.add("echo", "a", clash_at - 2, clash_at - 1, clash_at, _THROW_ECHO) else []


def _flavor_choices(m: _Mix, place: _Place, probe: dict) -> list[_Choice]:
    """How well each phrase-mix flavour fits this blend (the plain phrase mix is the baseline, and a flavour needs a
    clear reason to beat it):
    vocal throw: A finishes a sung phrase in the second half while B is instrumental there (echo styles step back
                 when A's vocal already goes into the echo to dodge a clash);
    roll (only when asked for, see _MANUAL_ONLY): B's drop / a big lift in B's energy lands on the 1 where A leaves,
          or B's main section kicks in at the bass swap (a slip roll into it);
    echo exit: A's last bars are sung and B leaves room for the repeats (no drop at A's exit);
    wash exit: A ends soft and tonal, or the keys sit a little apart (the wash smooths it);
    swell in: B opens on pads / atmosphere, or on a vocal;
    filter wash: A's second half is full (bass and music) over B's drums-only intro;
    dub echo: long blends where A's tail is sparse drums (best below 122 BPM, where the repeats have room)."""
    ga, gb = m.ga, m.gb
    L = place.overlap
    E = 4.0 * L
    if L < 4:
        return [_Choice(DEFAULT_STYLE, 0.6)]
    a_end = place.a0 + L
    tail = (a_end - 2, a_end)
    a_perc_tail = all(ga.perc[k] for k in range(*tail) if 0 <= k < ga.n)
    a_vocal_tail = _mean(ga.vocal, *tail)
    a_soft_tail = _mean(ga.energy, *tail) < 0.6 * _body_energy(ga)
    half = (place.a0 + L // 2, a_end)
    a_full_2nd = not all(ga.perc[k] for k in range(*half) if 0 <= k < ga.n) and _mean(ga.low, *half) >= _BASS_LOW
    b_at_e = _b_bar(place, L)
    b_drop_at_e = any(abs(b_at_e - d) <= 1 for d in gb.drops)

    def lift_at(k: int) -> float:  # B's energy over the 4 bars from overlap bar k vs the 4 bars before
        at = _b_bar(place, k)
        return _mean(gb.energy, at, at + 4) / max(0.05, _mean(gb.energy, _b_bar(place, max(0, k - 4)), at))

    lift = lift_at(L)
    lands = (place.swap is None or b_drop_at_e) and m.real_b
    sw = 4.0 * place.swap if place.swap is not None else None
    sw_lift = lift_at(place.swap) if place.swap is not None else 0.0
    b_intro_drums = gb.intro_drums and place.b0 < gb.intro[1]
    b_atmos = m.real_b and not gb.intro_drums and _mean(gb.energy, place.b0, place.b0 + 4) < 0.55 * _body_energy(gb)
    b_vocal_head = _mean(gb.vocal, place.b0, place.b0 + 4) >= transitions._VOCAL_ON
    clash = bool(_clash_bars(m, probe, place))
    ends = [p for p in _vocal_ends(m, probe, place) if E / 2 <= p <= E - 4.0
            and _mean(gb.vocal, _b_bar(place, int(p // 4)), _b_bar(place, int(p // 4)) + 2) < 0.4]
    real = m.real_a and m.real_b
    clean = a_perc_tail and b_intro_drums and L >= 16
    sparse_tail = a_perc_tail and a_vocal_tail < 0.3
    if not real:
        throw = (0.0, "needs both songs analysed")
    elif clash:
        throw = (0.0, "A's vocal already goes into the echo to dodge B's")
    elif ends:
        throw = (0.74, "A finishes a sung phrase over an instrumental part of B")
    else:
        throw = (0.0, "A doesn't finish a sung phrase over an instrumental part of B")
    if real and lands:
        roll = (0.8, "B's drop lands where A leaves") if lift >= 1.25 or b_drop_at_e else \
            (0.5, "B's lows come in where A leaves")
    elif real and sw is not None and 8.0 <= sw <= E - 4.0:
        roll = (0.62, "B's main section kicks in at the bass swap") if sw_lift >= 1.2 else \
            (0.48, "a slip roll into the bass swap")
    else:
        roll = (0.0, "nothing for a roll to land on")
    return [
        _Choice(DEFAULT_STYLE, 0.68 if clean else 0.6,
                "a clean drums-on-drums blend: nothing to hide" if clean else "the smooth, safe default"),
        _Choice("vocal throw", *throw),
        _Choice("roll", *roll),
        _Choice("echo exit", *((0.3, "A's vocal already goes into the echo") if clash
                               else (0.3, "B drops where A leaves: repeats would smear it") if b_drop_at_e
                               else (0.6, "A's last bars are sung: its last word can trail off") if a_vocal_tail >= 0.4
                               else (0.5, "A ends on music, no word to trail") if not a_perc_tail
                               else (0.42, "A ends on bare drums"))),
        _Choice("wash exit", *((0.66, "A ends soft and tonal: a wash dissolves it")
                               if real and not a_perc_tail and a_soft_tail
                               else (0.58, "the keys shift mood: a wash smooths it") if m.move == "diagonal"
                               else (0.5, "the keys sit a little apart") if m.move in ("+2 boost", "+7 boost")
                               else (0.32, "A's ending is busy: a wash would muddy it"))),
        _Choice("swell in", *((0.68, "B opens on pads / atmosphere") if b_atmos
                              else (0.6, "B opens on a vocal") if b_vocal_head
                              else (0.3, "B opens on drums: nothing for a swell to grow from"))),
        _Choice("filter wash", *((0.66, "A is still full (bass and music) over B's drums-only intro")
                                 if real and a_full_2nd and b_intro_drums else (0.34, "A's tail is already light"))),
        _Choice("dub echo", *((0.0, "needs a 16-bar blend") if L < 16
                              else (0.3, "A's vocal already goes into the echo") if clash
                              else (0.62, "slow, sparse drums at A's end leave room for long repeats")
                              if m.a_bpm < 122 and sparse_tail
                              else (0.46, "sparse drums at A's end") if sparse_tail
                              else (0.3, "A's end is busy or sung"))),
    ]


def _apply_flavor(style: str, m: _Mix, place: _Place, a_deck: dict, A: dict, B: dict, S: _Sends,
                  info: dict) -> tuple[str | None, list[dict]]:
    """The style's FX on top of a phrase mix (the plain phrase mix adds nothing), each one set from the two songs:
    the echo time from the tempo, the echo level and its number of repeats from how loud B is where they land, the
    swell and wash levels from how sparse B's intro / A's tail are, the filter climb from the blend's length.
    Returns (note, markers)."""
    ga, gb = m.ga, m.gb
    L = place.overlap
    E = 4.0 * L
    beats = _echo_beats(m.a_bpm)
    if style == "echo exit":
        e = _echo_shape(m, place, L)
        # A stays audible to the cut (its mids and highs already eased down) so the echo is heard coming off it
        A["volume"] = [(E / 2, 1.0, "ease"), (E - 1.0, 0.7, "hold"), (E, 0.0, "hold")]
        info.update(_throw(S, B, E, e))
        return f"A rides out the phrase and its last beat goes into a {_echo_words(e.beats)} echo: about " \
               f"{_repeats(e.fb)} repeats fade out over B" + (f", whose mids dip {e.duck:g} dB to let them through."
                                                               if e.duck else "."), []
    if style == "swell in":
        dens = _mean(gb.energy, place.b0, place.b0 + 4) / _body_energy(gb) if m.real_b else 0.5
        verb = round(min(0.18, max(0.1, 0.2 - 0.12 * dens)), 2)  # the sparser B's intro, the more wash
        rise = min(max(8.0, E / 2), 16.0 if dens < 0.4 else 8.0)
        S.add("verb", "b", -2.0, 2.0, 2.0 + rise / 2, verb)  # pre-fader: the wash sounds before B's fader is up
        B["volume"] = [(0.0, 0.0, "ease"), (rise, 1.0, "hold")]
        return f"B rises out of a reverb wash and its dry signal comes up over {int(rise // 4)} bars " \
               f"({'a sparse' if dens < 0.4 else 'a fuller'} intro, so {'more' if verb >= 0.14 else 'less'} wash).", \
            [{"beat": 0.0, "label": "B swell"}]
    if style == "wash exit":
        dens = _mean(ga.energy, place.a0 + L - 2, place.a0 + L) / _body_energy(ga) if m.real_a else 0.8
        verb = round(min(0.2, max(0.12, 0.24 - 0.1 * dens)), 2)
        S.add("verb", "a", E - 8.0, E, E + 2.0, verb)
        return f"A dissolves into a reverb wash over its last 2 bars ({'light' if verb < 0.15 else 'fuller'} " \
               "for this ending).", [{"beat": E - 8.0, "label": "reverb wash"}]
    if style == "dub echo":
        build = 16.0 if L >= 16 else 8.0
        info.update(echo_beats=beats, feedback=0.72)
        S.add("echo", "a", E - build, E - 1.0, E, 0.45)
        return f"A's last {int(build // 4)} bars build into a {_echo_words(beats)} dub echo (feedback 0.72) that " \
               "keeps repeating over B and fades away.", [{"beat": E - build, "label": "echo build"}]
    if style == "roll":
        lands = place.swap is None or any(abs(_b_bar(place, L) - d) <= 1 for d in gb.drops)
        at = E if lands else 4.0 * place.swap
        r = _roll_into(a_deck, at, 1, tail=1.0 if lands else 0.0)
        if lands and place.swap is None:
            A["eq_low"] = [(E - 2.0, 0.0, "linear"), (E - 1.0, _KILL, "hold")]
            A["filter"] = [(E - 8.0, 0.0, "ease"), (E, 0.35, "hold")]
        S.add("echo", "a", at - 1.0, at - 0.25, at + (2.0 if lands else 0.0), 0.35)
        where = "the 1 where B's drop lands" if lands else "the bass swap"
        return f"A rolls its last bar (2 → 1 → ½ → ¼ beat) into {where}.", [{"beat": r, "label": "loop roll"}]
    if style == "vocal throw":
        ends = [p for p in _vocal_ends(m, a_deck, place) if E / 2 <= p <= E - 4.0]
        if not ends:
            return None, []
        p = ends[-1]
        e = _echo_shape(m, place, p / 4.0)
        S.add("echo", "a", p - 1.05, p - 1.0, p, e.send)
        _duck_mids(B, p, e.duck)
        info.update(echo_beats=e.beats, feedback=e.fb)
        return f"A's last sung phrase in the blend (ending in bar {int(p // 4)}) goes into a " \
               f"{_echo_words(e.beats)} echo: about {_repeats(e.fb)} repeats trail off over B.", \
            [{"beat": p - 1.0, "label": "echo throw"}]
    if style == "filter wash":
        climb = min(E / 2, 32.0)
        A["filter"] = [(E - climb, 0.0, "ease"), (E, 0.55, "hold")]
        S.add("verb", "a", E - 4.0, E, E + 2.0, 0.2)
        return f"A climbs a high-pass over its last {int(climb // 4)} bars into a light reverb wash on its way out.", \
            [{"beat": E - 4.0, "label": "reverb wash"}]
    return None, []


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
        what = f"{L}-bar loop roll: B in at A {t0} (bar {a0 + 1}) from B {tb}; {looped}then A rolls " \
               f"{_ROLL_WORDS[roll[1]] if roll else '1 bar → 2 beats → 1 → ½'} and is cut on the 1 as B's bass drops in"
    elif tech in ("slam", "spinback"):
        into = "its drop" if b0 in gb.drops else "its main section" if b0 == gb.intro[1] else "its cue"
        if tech == "spinback":
            fx = "A is spun back on its last 2 beats"
        elif b0 in gb.drops:
            fx = "a clean cut after a short filter riser" + (f" and {rolled}" if rolled else "")
        else:
            fx = (f"{rolled} under " if rolled else "") + \
                 f"a high-pass riser over its last {max(2, min(bars, 8))} bars and an echo throw"
        what = f"{tech}: {fx}, and on the 1 at A {t0} (bar {a0 + 1}) B slams in at {into} (B {tb})"
    elif tech == "double drop":
        what = f"double drop: B comes in under A's build 8 bars before A's last drop (A {t0}, bar {a0 + 1}); A's " \
               f"bass drops out for a beat and both drop together on the 1 at A {_mmss(ga, a0 + 8)} with B's bass " \
               f"held back; the bass goes to B 4 bars later and A is out by {te}"
    elif tech == "brake":
        n = int(info["fx"]["beats"]) if info.get("fx") else 4
        what = f"brake: A powers down like a turntable switched off over its last {n} beats into the 1 at A {t0} " \
               f"(bar {a0 + 1}); B starts on that 1 from B {tb}"
    elif tech == "drop swap":
        what = "drop swap: let A's build run" + (" into a 1-bar stutter" if roll else "") + \
               f" and on the 1 where A would drop (A {t0}, bar {a0 + 1}) cut into B's drop at {tb}, which lands dry"
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
               f"from bar {L // 4 + 1}, and A leaves clean on the phrase."
    if tech in ("bass swap", "phrase blend"):
        return f"B's highs and mids open over the first {max(1, L // 2)} bars with its lows killed; A's mids and " \
               f"highs ease down over the second half under a slowly rising high-pass, and the bass changes hands on " \
               f"the 1."
    if tech == "filter sweep":
        return "Classic filter crossfade: A's high-pass climbs over the whole blend into a light wash at the top, " \
               "B's low-pass opens over the first half, the bass swaps on the 1."
    if tech == "echo out" and L:
        return "A keeps its fader up under a slowly rising high-pass; a dotted-8th echo catches its last 2 beats " \
               "and rings over B for a couple of bars after the cut."
    if tech == "loop roll":
        return "B stays bass-free until the roll ends; during the roll A's lows go while a high-pass rises, and a " \
               "dotted-8th echo catches its last half beat."
    if tech == "double drop":
        return "Through the double drop B plays without its lows (A's bass carries) and with its mids a touch down, " \
               "so the two drops stack without mud."
    if tech == "brake":
        return "The brake is a varispeed: A's pitch drops with its speed like a turntable powering down."
    if tech == "spinback":
        return "A whips backwards and winds down into a short wash; B lands dry on the 1."
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


# --- chill planner ---------------------------------------------------------------------------------------------------
#
# MODE "chill": every transition is shaped with the EQ, the filter and, once a track's vocals are split, its vocal
# stem; no echo, reverb, loop rolls, brakes or spinbacks. The default phrase mix runs 16 bars: B's fader is up with its
# EQ shut, its highs open first, then its mids, the bass changes hands halfway (a hard swap on the 1, a quick 1-bar
# swap or a slow 4-bar blend, picked from the two low ends), then A's mids and highs leave under a touch of high-pass.
# Two vocals never sound at once (with stems the outgoing vocal is muted, without them A's mids duck). When A's clean
# ending is too short for the blend, its last 8 bars repeat under B with the vocal taken out of the repeat. The other
# styles only step in when the songs call for them: an acapella where A's vocal can ride over B's beat, a filter fade
# for two full arrangements or keys a step apart, a double drop where the drops line up, and a clean cut on the
# phrase when the two can't blend. Clashing keys get an 8-bar blend with A's melody out early. There is no variety
# rule: the same style can run a whole set when it's what the songs want.

CHILL_STYLES = ("phrase mix", "acapella", "filter fade", "double drop", "quick cut")
SWAP_KINDS = ("hard", "quick", "slow")
_CHILL_BARS = 16
_CLASH_BARS = 8
_CHILL_MAX_GAP = 0.12  # wider tempo gaps can't glide inside a blend: a clean cut on the phrase instead
_HPF_TOUCH = 0.45  # A's high-pass by the end of a blend (~330 Hz): it thins out rather than just getting quieter
_ROOM_DB = -4.0  # A's highs / mids dip this much while B's open, so the overlap doesn't bump up in level
_LOOP_BARS = 8
_MILD_KEYS = ("+2 boost", "+7 boost", "diagonal")
_FOUR_FLOOR = 0.75  # kick_per_bar at/above this = a steady kick on every beat
_SOFT_LOW = 0.45  # low_per_bar below this = a soft low end (blends slowly without mud)
_LONG_BASS = 0.85  # bass_per_bar at/above this = a bassline that never lets go (808, reese): no hard cut
_TECH_STYLE = {"filter sweep": "filter fade", "double drop": "double drop", "quick cut": "quick cut",
               "drop swap": "quick cut", "slam": "quick cut", "brake": "quick cut", "spinback": "quick cut"}
_SWAP_HOW = {"hard": "hard bass swap on the 1 of bar {bar}", "quick": "quick 1-bar bass swap into bar {bar}",
             "slow": "slow 4-bar bass blend around bar {bar}"}  # fmt: skip
_SWAP_NAME = {"hard": "a hard swap on the 1", "quick": "a quick 1-bar swap", "slow": "a slow 4-bar blend"}


@dataclass
class _Chill:
    style: str
    tech: str
    place: _Place
    bars: int
    sw: int = 0  # overlap bar of the bass swap: the EQ stagger turns around it
    swap_kind: str = "hard"  # hard | quick | slow, or carry (B brings no bass yet: A's lows ride to the end)
    swap_why: str = ""
    key_mode: str = "normal"  # normal | mild (a step apart on the wheel) | clash
    loop: dict | None = None  # A's repeat, a slip loop on the master timeline
    loop_sung: bool = False  # the repeated bars have a vocal in them (taken out of the repeat when split)
    clean_tail: int = 0  # bars of A's ending after its last vocal (loop note)
    into_drop: bool = False  # B is cued so its drop lands where A leaves: the lows change hands there
    acapella_end: int | None = None  # overlap bar where A's vocal line ends (acapella)
    notes: list[str] = field(default_factory=list)
    fit: list[_Choice] = field(default_factory=list)


def _art(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _key_mode(m: _Mix) -> str:
    return "clash" if m.move == "clash" else "mild" if m.move in _MILD_KEYS else "normal"


def _probe(m: _Mix, loop: dict | None) -> dict:
    """A stand-in for deck A (its grid and loop) to read its bars through a repeat."""
    return {"beat0_s": 0.0, "native_bpm": m.a_bpm, "loops": [loop] if loop else []}


def _overlap_vocals(m: _Mix, place: _Place, loop: dict | None, strip: bool) -> tuple[list[float], list[float]]:
    """Vocal level per overlap bar on both decks; A is read through its repeat, which is silent when `strip` (the
    vocal is taken out of it)."""
    probe = _probe(m, loop)
    va, vb = [], []
    for k in range(place.overlap):
        ab, bb = _a_bar(m, probe, place, k), _b_bar(place, k)
        x = m.ga.vocal[ab] if 0 <= ab < m.ga.n else 0.0
        if strip and loop is not None and loop["start_beat"] <= 4.0 * k < loop["end_beat"]:
            x = 0.0
        va.append(x)
        vb.append(m.gb.vocal[bb] if 0 <= bb < m.gb.n else 0.0)
    return va, vb


def _loop_place(m: _Mix, place: _Place, L: int) -> tuple[_Place, dict, bool, int] | None:
    """When A's clean ending (after its last sung bar, on its 4-bar grid) is shorter than the blend and the blend
    would otherwise run over A's vocals or past its end: start the blend on that clean ending and repeat A's last 8
    bars under B until A is out. Needs a steady ending (drums all the way, no fade) and, when those 8 bars are sung,
    A's vocal stem (the repeat drops the vocal). Returns (placement, loop, the repeat is sung, clean bars) or None."""
    ga, gb = m.ga, m.gb
    ON = transitions._VOCAL_ON
    if not (m.real_a and m.real_b) or L < _LOOP_BARS:
        return None
    q = ga.off + (ga.end - ga.off) // 4 * 4  # A's music end on its 4-bar grid
    if q < 2 * _LOOP_BARS:
        return None
    sung = [k for k in range(min(q, ga.n)) if ga.vocal[k] >= ON]
    t0 = sung[-1] + 1 if sung else 0
    t0 = min(q, ga.off - (-(t0 - ga.off) // 4) * 4)
    clean = q - t0
    if clean >= L:
        return None
    sung_in = sum(1 for k in range(place.a0, place.a0 + L) if 0 <= k < ga.n and ga.vocal[k] >= ON)
    if sung_in < 2 and place.a0 + L <= ga.end:
        return None
    blk = (q - _LOOP_BARS, q)
    en, lo = ga.energy[blk[0] : blk[1]], ga.low[blk[0] : blk[1]]
    if not en or min(en) < 0.55 * max(en) or sum(lo) / len(lo) < 0.35:
        return None
    blk_sung = any(ga.vocal[k] >= ON for k in range(*blk))
    if blk_sung and not m.stems_a:
        return None
    a0 = t0 if clean >= _LOOP_BARS else q - _LOOP_BARS
    first = q - a0  # bars A plays before the repeat starts
    if first >= L or place.b0 + L > gb.n:
        return None
    loop = {"start_beat": 4.0 * first, "end_beat": 4.0 * L, "source_beat": 4.0 * (blk[0] - a0),
            "length_beats": 4.0 * _LOOP_BARS}
    return _Place(a0, place.b0, L, _swap(gb, place.b0, L)), loop, blk_sung, clean


def _base_place(m: _Mix, eng: tuple[str, _Place], L: int) -> tuple[_Place, list[str]]:
    """The engine's / the best phrase-aligned window of L bars, not over A's breakdown before its last drop."""
    place = _blend_place(m, L, eng)
    place, note = _clear_a_drop(m, place)
    return place, [note] if note else []


def _chill_place(m: _Mix, base: _Place, L: int) -> tuple[_Place, dict | None, bool, int, bool, list[str]]:
    """Where a chill blend of L bars goes from the base window: A's ending repeated when it's too short (see
    _loop_place), and B cued so a drop just after the blend lands where A leaves. Returns (placement, loop, repeat
    sung, clean tail bars, into B's drop, notes)."""
    notes: list[str] = []
    place = base
    loop, sung, clean = None, False, 0
    if (lp := _loop_place(m, place, L)) is not None:
        place, loop, sung, clean = lp
    place2, note = _land_on_drop(m, place, fixed_len=True)
    if note is not None:
        notes.append(note)
        if loop is None:
            place = place2
        elif place2.b0 + L <= m.gb.n:
            place = _Place(place.a0, place2.b0, L, None)
    return place, loop, sung, clean, note is not None, notes


def _swap_kind(m: _Mix, place: _Place, sw: int) -> tuple[str, str]:
    """hard / quick / slow, from the two low ends around overlap bar `sw`: a soft low end on either side blends slowly
    without mud; steady kicks with tight bass on both sides swap on the 1; a bassline that never lets go between the
    kicks, or a kick that isn't on every beat, crosses over one bar so two basslines never stack for long."""
    ga, gb = m.ga, m.gb
    if not (m.real_a and m.real_b):
        return "hard", "The bass swaps on the 1 (the songs aren't analysed yet, so this is the safe default)."
    ar = (place.a0 + max(0, sw - 4), place.a0 + max(1, sw))
    br = (place.b0 + sw, place.b0 + sw + 4)
    a_low, b_low = _mean(ga.low, *ar), _mean(gb.low, *br)
    if min(a_low, b_low) < _SOFT_LOW:
        who = "A's" if a_low <= b_low else "B's"
        return "slow", f"{who} low end is soft here, so the lows blend over 4 bars without getting muddy."
    a_bass = _mean(ga.bass, *ar) if ga.bass else 0.0
    b_bass = _mean(gb.bass, *br) if gb.bass else 0.0
    if max(a_bass, b_bass) >= _LONG_BASS:
        who = "A's" if a_bass >= b_bass else "B's"
        return "quick", f"{who} bassline barely lets go between the kicks, so the lows cross over one bar instead of " \
            "being chopped on the 1."
    a_kick = _mean(ga.kick, *ar) if ga.kick else 1.0
    b_kick = _mean(gb.kick, *br) if gb.kick else 1.0
    if min(a_kick, b_kick) >= _FOUR_FLOOR:
        return "hard", "Steady kicks and tight bass on both sides, so the bass swaps right on the 1."
    who = "A's" if a_kick <= b_kick else "B's"
    return "quick", f"{who} kick isn't on every beat, so the lows cross over one bar rather than switching on the 1."


def _acapella_fit(m: _Mix, va: list[float], vb: list[float], sw: int) -> tuple[float, str, int | None]:
    """(score, why, overlap bar where A's line ends): A sings across the bass swap (a line running at the swap, or
    starting within a bar of it, that ends inside the blend at least 2 bars after the swap), the keys sit together, the
    tempos match, A's vocal is split, and B doesn't sing before A's line is over (or B's vocal can wait: split too)."""
    ON = transitions._VOCAL_ON
    L = len(va)
    if not (m.real_a and m.real_b):
        return 0.0, "needs both songs analysed", None
    if not m.stems_a:
        return 0.0, "needs A's vocal stem (splitting runs in the background)", None
    if m.move not in _GOOD_KEYS:
        return 0.0, "the keys don't sit together, so A's vocal would clash with B's music", None
    if abs(m.b_bpm / m.a_bpm - 1) > _SMALL_GAP:
        return 0.0, "the tempos are too far apart for A's vocal to sit on B's beat", None
    start = next((k for k in range(max(0, sw - 1), min(L, sw + 2)) if va[k] >= ON), None)
    if start is None:
        return 0.0, "A isn't singing where its beat would drop out", None
    end = next((j for j in range(start, L) if va[j] < ON), None)
    if end is None:
        return 0.0, "A's vocal line runs past the end of the blend", None
    if end - max(start, sw) < 2:
        return 0.0, "A's line ends right at the swap", None
    if any(vb[k] >= ON for k in range(min(L, end + 1))) and not m.stems_b:
        return 0.0, "B sings before A's line is over (and B's vocal isn't split yet to wait)", None
    return 0.76, f"A sings over the bass swap (to bar {end}) and the keys match: its vocal can ride over B's beat", end


def _chill_choices(m: _Mix, place: _Place, key_mode: str, va: list[float], vb: list[float], sw: int, gap: float,
                   can_blend: bool) -> tuple[list[_Choice], int | None]:
    """How well each chill style fits this pair (the phrase mix is the baseline; the others need a reason), plus the
    overlap bar where A's line ends for an acapella."""
    ga, gb = m.ga, m.gb
    L = place.overlap
    dd = _double_drop_place(m)
    dd_choice = _Choice("double drop", *((_double_drop_score(m, dd), "both drops line up in key at the same tempo")
                                         if dd is not None else (0.0, "no pair of drops lines up in key at the same "
                                                                       "tempo")))
    if not can_blend:
        why = f"the tempos are {gap * 100:.0f}% apart, too far to glide inside a blend"
        return [_Choice("quick cut", 0.9, f"{why}: a clean cut on the phrase"), _Choice(DEFAULT_STYLE, 0.0, why),
                _Choice("acapella", 0.0, why), _Choice("filter fade", 0.0, why), dd_choice], None
    real = m.real_a and m.real_b
    clean = real and ga.outro_drums and place.a0 >= ga.outro[0] and gb.intro_drums and place.b0 + L <= gb.intro[1]
    if key_mode == "clash":
        base = (0.62, "the keys clash, so it's a short blend with A's melody out early")
    elif clean:
        base = (0.68, "a clean drums-on-drums blend: nothing to hide")
    else:
        base = (0.62, "the smooth default: B's highs, then its mids, then the bass")
    a_score, a_why, a_end = _acapella_fit(m, va, vb, sw)
    # two full arrangements colliding: A has no real outro (or the blend's first half is still in its main part)
    # and B's main part arrives in the first half (no real intro): the EQ alone would stack two whole songs
    a_full = real and (ga.outro_bars < 4 or place.a0 + L // 2 < ga.outro[0])
    b_full = real and (gb.intro_bars < 4 or place.b0 + L // 2 > gb.intro[1])
    if key_mode == "clash":
        ff = (0.3, "the keys clash: even a filter fade would show it, a short EQ blend hides it better")
    elif a_full and b_full:
        ff = (0.66, "two full arrangements meet here (A has no real outro, B no real intro): a filter fade melts one "
                    "into the other")
    elif key_mode == "mild":
        ff = (0.58, "the keys are a step apart: the filter would soften the change")
    else:
        ff = (0.4, "A's outro and B's intro leave room: the EQ alone blends them cleanly")
    return [
        _Choice(DEFAULT_STYLE, *base),
        _Choice("acapella", a_score, a_why),
        _Choice("filter fade", *ff),
        dd_choice,
        _Choice("quick cut", 0.3, "the songs can blend, so a cut would be abrupt"),
    ], a_end


def _decide_chill(m: _Mix, eng: tuple[str, _Place], technique: str | None, bars: int | None, style: str | None,
                  swap: str | None) -> _Chill:
    """Style, placement, bass swap and key handling for this pair: the best fit for the two songs, or what was asked
    for when it's possible here."""
    e_tech, _e = eng
    if style is not None and style not in STYLES:
        raise ValueError(f"unknown style {style!r}")
    if swap is not None and swap not in SWAP_KINDS:
        raise ValueError(f"unknown bass swap {swap!r}")
    notes: list[str] = []
    gap = abs(m.b_bpm / m.a_bpm - 1)
    can_blend = gap <= _CHILL_MAX_GAP
    key_mode = _key_mode(m)
    want = None
    if style in CHILL_STYLES:
        want = style
    elif style is not None:
        notes.append(f"{_art(style).capitalize()} {style} is one of the FX styles, which are switched off in chill "
                     "mixes.")
    elif technique is not None:
        want = _TECH_STYLE.get(technique, DEFAULT_STYLE)
    L = bars or (_CLASH_BARS if key_mode == "clash" else _CHILL_BARS)
    base, pnotes = _base_place(m, eng, L)

    def halfway(p: _Place) -> int:
        return p.swap if p.swap is not None and L // 4 <= p.swap <= (3 * L) // 4 else L // 2

    # an acapella rides A's vocal across the swap of the plain window: it wins over repeating A's ending
    va, vb = _overlap_vocals(m, base, None, strip=False)
    a_score, a_why, a_end = _acapella_fit(m, va, vb, halfway(base))
    loop, sung, clean, into_drop = None, False, 0, False
    if can_blend and a_score > 0 and want in (None, "acapella"):
        place = base
    else:
        place, loop, sung, clean, into_drop, more = _chill_place(m, base, L)
        pnotes += more
        va, vb = _overlap_vocals(m, place, loop, strip=m.stems_a)
    sw = halfway(place)
    choices, _a_end = _chill_choices(m, place, key_mode, va, vb, sw, gap, can_blend)
    if loop is None and place is base:
        choices = [c if c.style != "acapella" else _Choice("acapella", a_score, a_why) for c in choices]
    best = max(choices, key=lambda c: (c.score, c.style == DEFAULT_STYLE))
    pick = best
    if want is not None:
        asked = next(c for c in choices if c.style == want)
        if asked.score > 0 or want == "quick cut":
            pick = asked
        else:
            notes.append(f"{_art(want).capitalize()} {want} doesn't fit this pair ({asked.why}), so it's "
                         f"{_art(best.style)} {best.style}.")
    d = _Chill(pick.style, "quick cut", place, L, sw=sw, key_mode=key_mode, notes=notes, fit=choices)
    if pick.style == "quick cut":
        d.place = _cut_place(m, "drop swap" if e_tech == "drop swap" else "quick cut", eng)
        d.bars = bars or 4
        if not can_blend:
            d.notes.append(f"The tempos are {gap * 100:.0f}% apart, too far to glide inside a blend, so A cuts to B "
                           "on the phrase.")
        return d
    if pick.style == "double drop":
        d.tech, d.place, d.bars, d.sw = "double drop", _double_drop_place(m), 16, 12
        return d
    d.tech = {"filter fade": "filter sweep", "acapella": "bass swap"}.get(pick.style) or _blend_kind(m, place)
    d.loop, d.loop_sung, d.clean_tail, d.into_drop = loop, sung, clean, into_drop
    d.notes += pnotes
    if pick.style == "acapella":
        d.acapella_end, d.swap_kind = a_end, "hard"
    elif swap is not None:
        d.swap_kind, d.swap_why = swap, f"Bass swap picked by hand: {_SWAP_NAME[swap]}."
    elif into_drop:
        d.swap_kind, d.swap_why = "hard", "A's bass rides to the end and B's comes in with its drop on the 1."
    elif place.swap is None:
        d.swap_kind, d.swap_why = "carry", "B brings no bass during the blend, so A's lows carry it and leave with A."
    else:
        d.swap_kind, d.swap_why = _swap_kind(m, place, sw)
    return d


def _swap_lanes(A: dict, B: dict, at: float, kind: str, E: float) -> None:
    """The lows change hands at master beat `at`: hard (on the 1), quick (over the bar before it, -3 dB each at the
    crossing), slow (over the 4 bars around it), or carry (B brings no bass yet: A's lows ride to the end and B's open
    up over the second half)."""
    if kind == "quick":
        A["eq_low"] = [(at - 4.0, 0.0, "linear"), (at - 2.0, -3.0, "ease"), (at, _KILL, "hold")]
        B["eq_low"] = [(at - 4.0, _KILL, "ease"), (at - 2.0, -3.0, "linear"), (at, 0.0, "hold")]
    elif kind == "slow":
        A["eq_low"] = [(at - 8.0, 0.0, "linear"), (at, -3.0, "ease"), (at + 8.0, _KILL, "hold")]
        B["eq_low"] = [(at - 8.0, _KILL, "ease"), (at, -3.0, "linear"), (at + 8.0, 0.0, "hold")]
    elif kind == "carry":
        A["eq_low"] = [(E - 8.0, 0.0, "ease"), (E, _KILL, "hold")]
        B["eq_low"] = [(E / 2, _KILL, "ease"), (E, 0.0, "hold")]
    else:
        A["eq_low"] = [(0.0, 0.0, "hold"), (at, _KILL, "hold")]
        B["eq_low"] = [(0.0, _KILL, "hold"), (at, 0.0, "hold")]


def _b_in(B: dict, half: float, E: float) -> None:
    """B's fader is up from the start with its EQ shut: its highs open over the first quarter (the hats come in
    first), its mids over the second (up to `half`, where the bass changes hands)."""
    B["volume"] = [(0.0, 1.0, "hold")]
    B["eq_high"] = [(0.0, _KILL, "ease"), (max(4.0, min(E / 4, half)), 0.0, "hold")]
    B["eq_mid"] = [(half / 2, _KILL, "ease"), (max(half / 2 + 4.0, half), 0.0, "hold")]


def _a_out(A: dict, half: float, E: float, key_mode: str) -> None:
    """A makes room as B's bands come in (its highs dip _ROOM_DB while B's open, its mids while B's do), then after the
    swap at `half` its mids leave (a quarter of the way into the rest; right at the swap when the keys are a step
    apart), then its highs; its fader eases down over the last quarter of the rest and a touch of high-pass thins it
    out over the last half."""
    rest = max(8.0, E - half)
    m0 = half if key_mode == "mild" else half + 0.25 * rest
    hi_in = max(4.0, min(E / 4, half))
    A["eq_mid"] = [(half / 2, 0.0, "ease"), (max(half / 2 + 4.0, half), _ROOM_DB, "hold"), (m0, _ROOM_DB, "ease"),
                   (min(E, m0 + 0.375 * rest), _KILL, "hold")]
    A["eq_high"] = [(0.0, 0.0, "ease"), (hi_in, _ROOM_DB, "hold"), (E - 0.5 * rest, _ROOM_DB, "ease"), (E, _KILL, "hold")]
    A["volume"] = [(E - max(4.0, 0.25 * rest), 1.0, "ease"), (E, 0.0, "hold")]
    A["filter"] = [(E - 0.5 * rest, 0.0, "ease"), (E, _HPF_TOUCH, "hold")]


def _clash_shape(A: dict, B: dict, E: float) -> None:
    """Clashing keys: A's mids (chords and melody) leave in the blend's first quarter and B's only open once they're
    gone, so mostly drums overlap (A's vocal leaves with its mids, see _generate_chill)."""
    A["eq_mid"] = [(E / 8, 0.0, "ease"), (3 * E / 8, _KILL, "hold")]
    B["eq_mid"] = [(3 * E / 8, _KILL, "ease"), (5 * E / 8, 0.0, "hold")]


def _filter_fade(A: dict, B: dict, E: float) -> None:
    """B rises out of a closed low-pass that opens over the first three quarters while A slides up into a high-pass
    over everything after the first quarter; the bass still swaps (_swap_lanes)."""
    B["volume"] = [(0.0, 0.0, "ease"), (E / 4, 1.0, "hold")]
    B["filter"] = [(0.0, -0.7, "ease"), (0.75 * E, 0.0, "hold")]
    A["filter"] = [(E / 4, 0.0, "ease"), (E, 0.6, "hold")]
    A["volume"] = [(E - E / 8, 1.0, "ease"), (E, 0.0, "hold")]


def _acapella(A: dict, B: dict, s: float, e: float) -> None:
    """From the swap at `s` A's beat drops out (its instrumental stem, on the 1) and its vocal rides over B until the
    line ends at `e`, ringing a beat after; A's mids and highs stay open for the vocal, its lows went at the swap."""
    A["instrumental"] = [(s - 0.5, 1.0, "ease"), (s, 0.0, "hold")]
    A["vocals"] = [(e + 1.0, 1.0, "ease"), (e + 2.0, 0.0, "hold")]
    A["eq_mid"], A["eq_high"], A["filter"] = [], [], []
    A["volume"] = [(e + 2.0, 1.0, "ease"), (e + 3.0, 0.0, "hold")]


def _handoff(m: _Mix, va: list[float], vb: list[float], sw: int) -> tuple[tuple[float, float] | None, float | None,
                                                                            int | None, str | None]:
    """Never two vocals at once. Returns (A's vocal muted from beat t after an f-beat fade, B's vocal held back until
    beat t, overlap bar where A's mids duck instead because the vocal to quiet isn't split yet, note).

    B starts singing while A is between lines: A's vocal is muted there (inaudible) so its next line never starts.
    B starts singing mid-line before the bass swap: B's vocal waits for A's line to end, at the latest for the swap,
    where the vocals change hands with the bass. Later: A's vocal fades out just before its line, or over the 2 beats
    before B's, unless A's line ends within 2 bars (then B's vocal waits for it)."""
    ON = transitions._VOCAL_ON
    L = len(va)
    kb = next((k for k in range(L) if vb[k] >= ON), None)
    if kb is None:
        return None, None, None, None
    later = [j for j in range(kb, L) if va[j] >= ON]
    if not later:
        return None, None, None, None
    if va[kb] < ON:
        if m.stems_a:
            return (4.0 * kb, 1.0), None, None, \
                f"A's next vocal line (bar {later[0] + 1}) would land on B's, so A's vocal is muted from bar " \
                f"{kb + 1}, where A is between lines, and A plays on as an instrumental."
        return None, None, later[0], None
    end = next((j for j in range(kb, L) if va[j] < ON), L)
    if m.stems_b and (kb < sw or end <= kb + 2):
        t = min(end, sw) if kb < sw else end
        a_off = m.stems_a and any(va[j] >= ON for j in range(t, L))
        if t < end:
            note = f"B starts singing in bar {kb + 1} while A is mid-line, so the vocals change hands with the bass " \
                   f"(bar {t + 1}): A's vocal goes out as B's comes in."
        else:
            note = f"B starts singing in bar {kb + 1} while A is mid-line, so B's vocal waits until A's line ends " \
                   f"(bar {t + 1})" + (" and A's vocal stays out after that." if a_off else ".")
        return ((4.0 * t, 1.0) if a_off else None), 4.0 * t, None, note
    if m.stems_a:
        gap = next((j for j in range(kb - 1, max(-1, kb - 5), -1) if va[j] < ON), None)
        if gap is not None:
            t, f, where = 4.0 * (gap + 1), 1.0, f"just before its line in bar {gap + 2}"
        else:
            t, f, where = 4.0 * kb, 2.0, f"over the 2 beats before bar {kb + 1}"
        return (t, f), None, None, \
            f"B starts singing in bar {kb + 1}, so A's vocal fades out {where} and A plays on as an instrumental."
    return None, None, kb, None


def _duck_a_mids(A: dict, k0: int, E: float) -> float:
    """No vocal stem to mute: A's mids duck to -18 dB over the 2 bars before overlap bar k0 (A's vocal lives there),
    then go on down with A's own fade. Returns the beat where the duck lands."""
    at = max(8.0, 4.0 * k0)
    end = max((p[0] for p in A["eq_mid"]), default=E)
    pts = [(at - 8.0, 0.0, "ease"), (at, _VOCAL_CUT, "hold")]
    if end > at + 4.0:
        pts += [(end - 4.0, _VOCAL_CUT, "ease"), (end, _KILL, "hold")]
    A["eq_mid"] = pts
    return at


def _decks(m: _Mix, place: _Place, E: float) -> tuple[dict, dict]:
    """Deck A (beat 0 = its bar a0, plays from its start to E) and deck B (cued at bar b0, plays to its end)."""
    ga, gb = m.ga, m.gb
    a_deck = {
        "track_id": m.a.id, "beat0_s": round(ga.fd + place.a0 * ga.bar_s, 4), "native_bpm": m.a_bpm,
        "play_from_beat": 0.0, "play_to_beat": E, "echo_beats": 0.75, "loops": [], "fx": [], "lanes": {},
    }  # fmt: skip
    b_beat0 = gb.fd + place.b0 * gb.bar_s
    b_deck = {
        "track_id": m.b.id, "beat0_s": round(b_beat0, 4), "native_bpm": m.b_bpm,
        "play_from_beat": max(0.0, round(-b_beat0 * m.b_bpm / 60.0, 4)), "play_to_beat": 0.0,
        "echo_beats": 0.75, "fx": [], "lanes": {}, "loops": [],
    }  # fmt: skip
    a_deck["play_from_beat"] = round(min(-1.0, -a_deck["beat0_s"] * m.a_bpm / 60.0), 4)
    b_end_s = m.b_dur if m.b_dur > 0 else gb.fd + gb.n * gb.bar_s
    b_deck["play_to_beat"] = round(max(E + 8.0, (b_end_s - b_beat0) * m.b_bpm / 60.0), 4)
    return a_deck, b_deck


def _preview(tempo: _Tempo, a_deck: dict, b_deck: dict, L: int, E: float) -> tuple[float, float]:
    """Preview window: 8-16 bars before B, 8-16 bars after A is out, ~90 s at most (pre-ramps shown when they fit)."""
    pre, post = (16 if L <= 8 else 8), (16 if L == 0 else 8)
    if tempo.pre_start is not None:
        pre = max(pre, int(-tempo.pre_start // 4))
    tk = [{"beat": bt, "value": v, "curve": c} for bt, v, c in tempo.keyframes]
    lo_beat = math.ceil(a_deck["play_from_beat"])
    hi_beat = math.floor(b_deck["play_to_beat"])

    def bounds(pre_bars: int, post_bars: int) -> tuple[float, float]:
        return max(lo_beat, -4.0 * pre_bars), min(hi_beat, E + 4.0 * post_bars)

    while master_seconds(tk, bounds(pre, post)[1], bounds(pre, post)[0]) > _MAX_PREVIEW_S:
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
    return bounds(pre, post)


def _chill_headline(m: _Mix, d: _Chill) -> str:
    ga, gb = m.ga, m.gb
    p = d.place
    a0, b0, L = p.a0, p.b0, p.overlap
    t0, tb, te = _mmss(ga, a0), _mmss(gb, b0), _mmss(ga, a0 + L)
    if d.style == "quick cut":
        what = f"clean cut on the 1 at A {t0} (bar {a0 + 1}) into B from {tb}"
    elif d.style == "double drop":
        what = f"double drop: B comes in under A's build 8 bars before A's last drop (A {t0}, bar {a0 + 1}); both " \
               f"drop together on the 1 at A {_mmss(ga, a0 + 8)} with B's bass held back, the bass goes to B 4 bars " \
               f"later and A is out by {te}"
    else:
        name = {"filter fade": "filter fade", "acapella": "acapella blend"}.get(d.style, "phrase mix")
        if d.into_drop:
            swap = f"A's bass rides to the end and B's lands with its drop on the 1 at A {te}"
        elif d.swap_kind == "carry":
            swap = "A's bass carries the blend (B brings none yet)"
        else:
            swap = _SWAP_HOW[d.swap_kind].format(bar=d.sw + 1) + f" (A {_mmss(ga, a0 + d.sw)})"
        out = f"A out by {te}" if d.loop is None else "A out once its repeat ends"
        what = f"{L}-bar {name}: B in at A {t0} (bar {a0 + 1}, on the phrase) from B {tb}, {swap}, {out}"
    edges = ""
    if m.tr is not None and m.tr.get("notes"):
        first = str(m.tr["notes"][0])
        if " → " in first:
            edges = first.split(" → ", 1)[0] + " → "
    text = edges + what + "."
    return text[0].upper() + text[1:]


def _chill_eq_note(m: _Mix, d: _Chill) -> str | None:
    L = d.place.overlap
    if d.style == "filter fade":
        return "B rises out of a closed low-pass that opens over the first three quarters while A slides up into a " \
               "high-pass; the bass still changes hands, no echo or reverb."
    if d.style != DEFAULT_STYLE or not L:
        return None
    if d.key_mode == "clash":
        keys = f" ({m.a.camelot} → {m.b.camelot})" if m.a.camelot and m.b.camelot else ""
        return f"The keys clash{keys}, so the blend is {L} bars and mostly drums overlap: A's mids (its chords and " \
               "melody) and its vocal leave in the first bars, and B's mids only open after they're gone."
    q = max(1, L // 4)
    mild = " (right at the swap: the keys are a step apart)" if d.key_mode == "mild" else ""
    return f"B's fader is up with its EQ shut: its highs (the hats) open over the first {q} bars and its mids over " \
           f"the next {q}, A's dipping a little to make room; after the swap A's mids leave{mild}, then its highs, " \
           "under a touch of high-pass."


def _generate_chill(a: Track, a_an: dict | None, b: Track, b_an: dict | None, folder: str, technique: str | None,
                    bars: int | None, transition: dict | None, style: str | None, swap: str | None) -> dict:
    m = _mix(a, a_an, b, b_an, transition)
    eng = _engine(m)
    d = _decide_chill(m, eng, technique, bars, style, swap)
    place = d.place
    L = place.overlap
    E = 4.0 * L
    ga = m.ga
    a_deck, b_deck = _decks(m, place, E)
    A, B = _lanes(), _lanes()
    body: list[str] = []  # notes on the loop, the vocals and A's ending
    markers = [{"beat": 0.0, "label": "B in" if L else "cut"}]
    a_mute: list[tuple[float, float]] = []  # (beat, fade): A's vocal is muted from there on
    b_wait: float | None = None
    if d.style == "quick cut":
        A["volume"] = [(-4.0, 1.0, "hold"), (0.0, 0.0, "hold")]
    elif d.style == "double drop":
        _double_drop_lanes(A, B)
        markers += [{"beat": 32.0, "label": "double drop"}, {"beat": 48.0, "label": "bass swap"}]
    else:
        half = 4.0 * d.sw
        low_at = E if d.into_drop else half
        if d.style == "filter fade":
            _filter_fade(A, B, E)
        else:
            _b_in(B, half, E)
            _a_out(A, half, E, d.key_mode)
        _swap_lanes(A, B, low_at, d.swap_kind, E)
        if d.swap_kind != "carry":
            markers.append({"beat": low_at, "label": "bass swap"})
        va, vb = _overlap_vocals(m, place, d.loop, strip=m.stems_a)
        if d.loop is not None:
            a_deck["loops"] = [d.loop]
            markers.append({"beat": d.loop["start_beat"], "label": "loop"})
            if d.loop_sung:
                a_mute.append((d.loop["start_beat"], 0.25))
            more = round(L - d.loop["start_beat"] / 4)
            again = "once" if more == _LOOP_BARS else f"for {more} more bars"
            clean = f"only {d.clean_tail} bars" if d.clean_tail else "sung right to the end"
            body.append(f"A's ending after its last vocal is {clean}, so its last {_LOOP_BARS} bars repeat {again} "
                        f"under B to give the blend its {L} bars" +
                        (" (the vocal is taken out of the repeat)." if d.loop_sung else "."))
        if d.style == "acapella" and d.acapella_end is not None:
            e = 4.0 * d.acapella_end
            _acapella(A, B, half, e)
            a_deck["play_to_beat"] = max(E, e + 4.0)
            if m.stems_b and any(x >= transitions._VOCAL_ON for x in vb):
                b_wait = e + 2.0
            markers.append({"beat": half, "label": "acapella"})
            body.append(f"Acapella: on the 1 of bar {d.sw + 1} A's beat drops out and its vocal rides over B's "
                        f"until the line ends (bar {d.acapella_end})" +
                        ("; B's vocal waits until then." if b_wait is not None else "."))
        else:
            clash_keys = d.key_mode == "clash" and d.style == DEFAULT_STYLE
            if clash_keys:
                _clash_shape(A, B, E)
                if m.stems_a and any(x >= transitions._VOCAL_ON for x in va):
                    a_mute.append((E / 4, E / 8))
            mute, wait, duck, note = _handoff(m, va, vb, d.sw)
            if mute is not None:
                a_mute.append(mute)
            if wait is not None:
                b_wait = wait
            if note:
                body.append(note)
            if duck is not None and not clash_keys:
                _duck_a_mids(A, duck, E)
                body.append(f"Vocals on both decks from bar {duck + 1}: A's vocal stem isn't split yet, so A's mids "
                            f"duck to {_VOCAL_CUT:.0f} dB there instead (just the vocal goes once splitting is done).")
        a_end = 4.0 * (ga.end - place.a0)  # A's music stops here
        if m.real_a and d.loop is None and a_end < E and d.style != "acapella":
            A["volume"] = [(max(0.0, a_end - 8.0), 1.0, "ease"), (max(1.0, a_end), 0.0, "hold")]
            body.append(f"A's music ends {round((E - a_end) / 4)} bars before the blend does, so A fades out early: "
                        "a shorter blend fits better.")
    if a_mute:
        t, f = min(a_mute)
        A["vocals"] = [(max(-4.0, t - f), 1.0, "ease"), (t, 0.0, "hold")]
        if not any(mk["label"] == "loop" and abs(mk["beat"] - t) < 1e-6 for mk in markers):
            markers.append({"beat": t, "label": "A vocal out"})
    if b_wait is not None:
        B["vocals"] = [(0.0, 0.0, "hold"), (b_wait, 0.0, "ease"), (b_wait + 2.0, 1.0, "hold")]
        markers.append({"beat": b_wait + 2.0, "label": "B vocal in"})
    tempo = _tempo(m, E, cut=L == 0)
    ws, we = _preview(tempo, a_deck, b_deck, L, E)
    a_deck["lanes"] = {lane: _kf(A[lane], ws, lane) for lane in LANES}
    b_deck["lanes"] = {lane: _kf(B[lane], ws, lane) for lane in LANES}
    tempo_kf = _kf(tempo.keyframes, ws, "volume")
    for k in tempo_kf:
        k["value"] = round(k["value"], 3)
    if L:
        markers.append({"beat": E, "label": "A out"})
    if tempo.pre_start is not None:
        markers.append({"beat": tempo.pre_start, "label": "tempo ramp"})
    if tempo.post_end is not None:
        markers.append({"beat": E, "label": "tempo ramp"})
    notes = [_chill_headline(m, d)]
    if (eq := _chill_eq_note(m, d)) is not None:
        notes.append(eq)
    if L and d.style != "double drop" and d.swap_why:
        notes.append(d.swap_why)
    notes += body + [tempo.note] + d.notes
    if not (m.real_a and m.real_b):
        missing = " and ".join(n for n, ok in (("A", m.real_a), ("B", m.real_b)) if not ok)
        notes.append(f"Rough plan from tag BPMs: {missing} {'is' if ' and ' not in missing else 'are'} not "
                     "analysed yet, so beats and phrases may not line up.")
    elif L and not (m.stems_a and m.stems_b):
        missing = " and ".join(n for n, ok in (("A", m.stems_a), ("B", m.stems_b)) if not ok)
        notes.append(f"Vocal stems for {missing} aren't split yet (that runs in the background); reopen this "
                     "transition afterwards for the cleaner vocal handoffs.")
    plan = {
        "version": PLAN_VERSION, "folder": folder, "from_id": a.id, "to_id": b.id, "technique": d.tech,
        "bars": d.bars, "key_lock": True, "window": {"start_beat": ws, "end_beat": we}, "tempo": tempo_kf,
        "a": a_deck, "b": b_deck, "markers": sorted(markers, key=lambda mk: mk["beat"]), "notes": notes,
        "style": d.style, "edited": False,
        "style_fit": [{"style": c.style, "fit": round(c.score, 2), "why": c.why}
                      for c in sorted(d.fit, key=lambda c: -c.score)],
        "swap_kind": d.swap_kind if d.swap_kind in SWAP_KINDS and L and d.style != "double drop" else None,
        "vocal_stems": {"a": m.stems_a, "b": m.stems_b},
    }  # fmt: skip
    return validate(plan)


# --- public API ----------------------------------------------------------------------------------------------------


@dataclass
class _Decision:
    tech: str
    place: _Place
    bars: int
    style: str = DEFAULT_STYLE
    into_drop: bool = False
    notes: list[str] | None = None
    fit: list[_Choice] | None = None  # how well each alternative style fits this pair (the editor ranks them)


def _structure(m: _Mix, eng: tuple[str, _Place], tech: str, bars: int | None) -> _Decision:
    """Placement for a technique: where B comes in and how long both play (no loops: each deck plays straight
    through, except in a loop roll)."""
    e_tech, e_place = eng
    notes: list[str] = []
    if tech == "double drop" and _double_drop_place(m) is None:
        notes.append("No pair of drops lines up for a double drop here, so this is a long blend instead.")
        tech = "long blend"
    big_gap = abs(m.b_bpm / m.a_bpm - 1) > _MAX_BLEND_GAP
    bars_given = bars is not None
    into_drop = False
    if tech in _CUTS or (tech == "echo out" and bars is None and (e_place.overlap == 0 or big_gap)):
        return _Decision(tech, _cut_place(m, tech, eng), bars or 4, notes=notes)
    if tech == "double drop":
        return _Decision(tech, _double_drop_place(m), 16, notes=notes)
    if tech == "loop roll":
        bars = bars or _default_bars(m, tech, e_place)
        if e_place.overlap and e_tech not in _CUTS:
            return _Decision(tech, _Place(e_place.a0, e_place.b0, bars, None), bars, notes=notes)
        cut = _cut_place(m, "quick cut", eng)
        return _Decision(tech, _Place(cut.a0, cut.b0, bars, None), bars, notes=notes)
    bars = bars or _default_bars(m, tech, e_place)
    place = _blend_place(m, bars, eng)
    place, note = _clear_a_drop(m, place)
    notes += [note] if note else []
    place, note = _land_on_drop(m, place, fixed_len=bars_given)
    if note:
        into_drop = True
        notes.append(note)
        if tech == "bass swap":
            tech = "long blend" if place.overlap >= 16 else "phrase blend"
        bars = place.overlap
    if place.a0 + place.overlap > m.ga.end and m.real_a:
        notes.append(f"A's music ends {place.a0 + place.overlap - m.ga.end} bars before the blend does, so A fades "
                     "out early: a shorter blend fits better.")
    return _Decision(tech, place, bars, into_drop=into_drop, notes=notes)


def _double_drop_score(m: _Mix, place: _Place) -> float:
    """0.7, or 0.84 when both drops really lift (energy at the drop vs the 8 bars before it)."""
    ga, gb = m.ga, m.gb
    da, db = place.a0 + 8, place.b0 + 8
    lift = min(_mean(ga.energy, da, da + 4) / max(0.05, _mean(ga.energy, da - 8, da)),
               _mean(gb.energy, db, db + 4) / max(0.05, _mean(gb.energy, db - 8, db)))
    return 0.84 if lift >= 1.3 else 0.7


def _decide(m: _Mix, eng: tuple[str, _Place], technique: str | None, bars: int | None, style: str | None,
            recent: list[str] | None) -> _Decision:
    """Technique, placement and style for this pair: the best fit for the two songs given the styles of the
    transitions before it (`recent`, most recent last). A requested technique / style wins when it's possible."""
    e_tech, e_place = eng
    recent = list(recent or [])
    if style is not None and style not in STYLES:
        raise ValueError(f"unknown style {style!r}")
    if technique is None and style is not None and style not in BLEND_STYLES:
        technique = style  # a structural style is its technique
    base = _base_technique(m, e_tech, e_place)
    options: list[tuple[_Choice, str]] = []  # (choice, technique that builds it)
    if base in ("cut", "drop swap"):
        options += [(c, c.style) for c in _exit_choices(m, eng, base)]
    elif base in _BLENDS:
        options.append((_Choice(DEFAULT_STYLE, 0.6, "the music leaves room for a phrase blend"), base))
    elif base == "echo out":  # the blend runs into A's cold ending: echo it out, or fade it out on the phrase
        options += [(_Choice("echo out", 0.62, "A stops cold at the end of the blend"), "echo out"),
                    (_Choice(DEFAULT_STYLE, 0.6, "fade A out on the phrase"), _blend_kind(m, e_place))]
    else:  # a filter sweep (awkward keys / A's vocal over B's instrumental), with a plain blend as the fallback
        options += [(_Choice(base, 0.66, "the keys or vocals clash: a filter crossfade hides it"), base),
                    (_Choice(DEFAULT_STYLE, 0.5, "a plain blend over the clash"), "bass swap")]
    if base not in ("cut", "drop swap") and (dd := _double_drop_place(m)) is not None:
        options.append((_Choice("double drop", _double_drop_score(m, dd), "both drops line up in key"), "double drop"))
    if technique is not None:
        d = _structure(m, eng, technique, bars)
    else:
        pick = _choose([c for c, _ in options], recent)
        first: list[str] = []
        _variety_note([c for c, _ in options], pick, recent, first)
        tech = next((t for c, t in options if c is pick), options[0][1])
        d = _structure(m, eng, tech, bars)
        d.notes = first + (d.notes or [])
    structural = [c for c, _ in options if c.style != DEFAULT_STYLE]
    if d.tech in _BLENDS and d.place.overlap:
        probe = {"beat0_s": 0.0, "native_bpm": m.a_bpm, "loops": []}
        choices = _flavor_choices(m, d.place, probe)
        fits = {c.style for c in choices if c.score > 0}
        d.fit = choices + structural  # the flavours, and the bigger moves that also suit these two
        if style in BLEND_STYLES:
            if style not in fits:
                why = next(c.why for c in choices if c.style == style)
                d.notes.append(f"A {style} doesn't fit this pair ({why}), so it's a plain phrase mix.")
            d.style = style if style in fits else DEFAULT_STYLE
        else:
            pick = _choose(choices, recent)
            _variety_note(choices, pick, recent, d.notes)
            d.style = pick.style
    else:
        if style in BLEND_STYLES and style != DEFAULT_STYLE:
            d.notes.append(f"A {style} needs a blend, and this pair is planned as a {d.tech}.")
        d.style = d.tech if d.tech in STYLES else DEFAULT_STYLE
        if d.tech in _CUTS or (d.tech == "echo out" and not d.place.overlap):
            d.fit = [c for c, _ in options] if base in ("cut", "drop swap") else _exit_choices(m, eng, base)
        else:
            d.fit = [c for c, _ in options]
    return d


def _variety_note(choices: list[_Choice], pick: _Choice, recent: list[str], notes: list[str]) -> None:
    """Say so when the best fit stepped aside because it was used in the last few transitions."""
    best = max((c for c in choices if c.style not in _MANUAL_ONLY), key=lambda c: c.score, default=None)
    if best is not None and best.style != pick.style and best.score > pick.score and best.style in recent[-4:]:
        notes.append(f"A {best.style} fits these two best, but the set just had one, so this one is a "
                     f"{pick.style} for variety.")


def plan_styles(pairs: list[tuple[Track, dict | None, Track, dict | None, dict | None]],
                fixed: list[str | None] | None = None) -> list[str]:
    """The style of every transition of a set, in order: each pair's best fit (FX mode: given the styles before it, no
    flashy style more than three times in a row; phrase mixes repeat freely). `pairs` are (a, a_an, b, b_an,
    transition); fixed[k] = the style of a saved plan (it stays)."""
    out: list[str] = []
    for k, (a, a_an, b, b_an, tr) in enumerate(pairs):
        if fixed and k < len(fixed) and fixed[k]:
            out.append(str(fixed[k]))
            continue
        try:
            m = _mix(a, a_an, b, b_an, tr)
            if MODE == "chill":
                out.append(_decide_chill(m, _engine(m), None, None, None, None).style)
            else:
                out.append(_decide(m, _engine(m), None, None, None, out[-4:]).style)
        except Exception:  # noqa: BLE001 - a pair that can't be planned keeps the chain going
            out.append(DEFAULT_STYLE)
    return out


def generate(
    a: Track,
    a_an: dict | None,
    b: Track,
    b_an: dict | None,
    folder: str,
    technique: str | None = None,
    bars: int | None = None,
    transition: dict | None = None,
    style: str | None = None,
    recent: list[str] | None = None,
    swap: str | None = None,
) -> dict:
    """MixPlan (types.ts) for A → B: the best-fitting technique and style for the two songs (or the requested ones)
    as beat-locked lane automation, loops, tempo moves and a preview window. `style` forces a style, `swap` a bass
    swap (chill mode: hard / quick / slow); `recent` = the styles of the transitions before this one in its set (FX
    mode only, see plan_styles). Works without analyses (rough plan from tag BPMs)."""
    if technique is not None and technique not in TECHNIQUES:
        raise ValueError(f"unknown technique {technique!r}")
    if bars is not None and bars not in BARS:
        raise ValueError(f"bars must be one of {BARS}")
    if MODE == "chill":
        return _generate_chill(a, a_an, b, b_an, folder, technique, bars, transition, style, swap)
    return _generate_fx(a, a_an, b, b_an, folder, technique, bars, transition, style, recent)


def _generate_fx(a: Track, a_an: dict | None, b: Track, b_an: dict | None, folder: str, technique: str | None,
                 bars: int | None, transition: dict | None, style: str | None, recent: list[str] | None) -> dict:
    """The FX planner's MixPlan (MODE "fx"): styles with echo / reverb sends, loop rolls and deck FX."""
    m = _mix(a, a_an, b, b_an, transition)
    eng = _engine(m)
    d = _decide(m, eng, technique, bars, style, recent)
    tech, place, bars, into_drop = d.tech, d.place, d.bars, d.into_drop
    notes_extra = d.notes or []
    L = place.overlap
    E = 4.0 * L
    ga, gb = m.ga, m.gb

    a_deck = {
        "track_id": a.id, "beat0_s": round(ga.fd + place.a0 * ga.bar_s, 4), "native_bpm": m.a_bpm,
        "play_from_beat": 0.0, "play_to_beat": E, "echo_beats": 0.75, "loops": [], "fx": [], "lanes": {},
    }  # fmt: skip
    b_beat0 = gb.fd + place.b0 * gb.bar_s
    b_deck = {
        "track_id": b.id, "beat0_s": round(b_beat0, 4), "native_bpm": m.b_bpm,
        "play_from_beat": max(0.0, round(-b_beat0 * m.b_bpm / 60.0, 4)), "play_to_beat": 0.0,
        "echo_beats": 0.75, "fx": [], "lanes": {}, "loops": [],
    }  # fmt: skip
    a_deck["play_from_beat"] = round(min(-1.0, -a_deck["beat0_s"] * m.a_bpm / 60.0), 4)
    b_end_s = m.b_dur if m.b_dur > 0 else gb.fd + gb.n * gb.bar_s
    b_deck["play_to_beat"] = round(max(E + 8.0, (b_end_s - b_beat0) * m.b_bpm / 60.0), 4)

    A, B = _lanes(), _lanes()
    S = _Sends()
    beats = _echo_beats(m.a_bpm)
    if tech in _CUTS or (tech == "echo out" and L == 0):
        into = place.b0 in gb.drops
        info = _cut_lanes(tech, bars, A, B, S, into, _echo_shape(m, place, 0, into))
    elif tech == "loop roll":
        info = _loop_roll(m, place, A, B, a_deck, S)
    elif tech == "double drop":
        info = _double_drop_lanes(A, B)
    else:
        info = _blend_lanes(tech, L, place.swap, A, B, S, into_drop, _echo_shape(m, place, L, into_drop))
    clash = _clash_bars(m, a_deck, place) if tech not in ("filter sweep", "double drop") else []
    clash_at = _cut_mids(A, clash[0], E) if clash and L else None
    a_out = 4.0 * (ga.end - place.a0)  # A's music ends here
    if m.real_a and L and tech not in ("loop roll", "double drop") and a_out < E:
        A["volume"] = [(max(0.0, a_out - 8.0), 1.0, "ease"), (max(1.0, a_out), 0.0, "hold")]  # out before it stops
    throws = _mid_sends(m, tech, place, a_deck, S, clash_at)  # the problem-solving throw first, then the style
    if throws and info["throw"] is None:
        info.update(echo_beats=beats, feedback=0.5)  # the clash throw: a short echo of A's last word
    flavor_note, flavor_marks = None, []
    if tech in _BLENDS and d.style != DEFAULT_STYLE:
        flavor_note, flavor_marks = _apply_flavor(d.style, m, place, a_deck, A, B, S, info)
        if flavor_note is None:
            d.style = DEFAULT_STYLE
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
    if L and tech not in ("loop roll", "double drop") and place.swap is not None:
        markers.append({"beat": 4.0 * place.swap, "label": "bass swap"})
    markers += flavor_marks
    if tech == "double drop":
        markers += [{"beat": 32.0, "label": "double drop"}, {"beat": 48.0, "label": "bass swap"}]
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
        notes.append(f"Vocals on both decks in {where} of the blend: A's mids are ducked to "
                     f"{_VOCAL_CUT:.0f} dB from bar {k0 + 1} and its vocal is thrown into the echo so they don't clash.")
    if flavor_note:
        notes.append(flavor_note)
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
        "style": d.style, "edited": False,
        "style_fit": [{"style": c.style, "fit": round(c.score, 2), "why": c.why}
                      for c in sorted(d.fit or [], key=lambda c: -c.score)],
    }  # fmt: skip
    return validate(plan)


def barno(beat: float) -> int:
    """1-based bar of the blend a master beat falls in."""
    return int(beat // 4) + 1
