"""Mix suggestions: concrete A → B transitions (B's cue, entry point in A, phrase-aligned overlap, technique) built
from the intro/outro analyses of musicreco.structure, plus set-level helpers (consecutive pairs, partners, ordering).

Bars in the output are A's bars: B is pitched to A's tempo, so bar k of the overlap is A bar a0+k and B bar b0+k.
A half/double-time B is put on a matching grid first (one of its bars = two of A's, or the other way round).
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterator
from dataclasses import dataclass

from . import mixing
from .models import Track

TECHNIQUES = ("long blend", "phrase blend", "bass swap", "drop swap", "quick cut", "echo out")
_CHILL = os.environ.get("SEEDBOX_MIX_MODE", "").strip().lower() != "fx"  # same switch as mixplan.MODE

PHRASE = 8  # bars; overlaps are 8, 16 or 32 bars and always start on a phrase in both tracks
_MAX_BARS = 20_000  # sanity cap on bar_count (a 2-hour track at 130 BPM is ~3,900 bars)
_LENGTHS = (32, 16, 8)
_MAX_CUT_BARS = 32  # never end the overlap more than this many bars before A's music ends
_VOCAL_ON = 0.5  # vocal_per_bar at/above this counts as a sung bar
_VOCAL_FLOOR = 0.3  # the soft clash only counts likelihood above this (hats/synths read as weak "vocals")
_BASS_ON = 0.5  # mean low_per_bar at/above this = kick/bass weight on that deck
_NO_BASS = 0.25  # below this B has no real low end in the overlap
_MAX_CLASH_BARS = 2  # a blend with more overlapping vocal bars than this is not offered
_MAX_TONAL_CLASH_BARS = 4  # with clashing keys, at most this many bars with music (not just drums) on both decks
_MIN_BLEND_FIT = 50.0
_LOUD = 0.85  # energy_per_bar of a full-power bar
_COLD_END_BARS = 2  # a full-power bar this close to A's last bar = A stops cold (no wind-down to ride out)

_HARMONIC_WEIGHT = 0.6  # score = 0.6 * mixing.score + 0.4 * structural fit
_NEUTRAL_FIT = 70.0  # structural fit assumed while an analysis is missing (keeps ready/not-ready scores comparable)
_CUT_FIT = {"drop swap": 62.0, "quick cut": 55.0, "echo out": 55.0}
_BIG_PITCH = 4.0  # % tempo change on B that earns a warning
_GRID_BPM_TOL = 0.03  # detected tempo further than this from tunebat's (x0.5/1/2) = probably a wrong download
_WRONG_FILE_FIT = 25.0  # structural fit lost per track whose audio doesn't match its tunebat tempo
_MIN_SKIP_NOTE = 4  # bars of A left unplayed before it's worth mentioning

# window penalties, per bar
_P_BODY_CUT = 2.5  # A's main section cut off before its outro
_P_OUTRO_SKIP = 0.4  # A outro bars never played
_P_INTRO_SKIP = 0.35  # B intro bars skipped by cueing later
_P_A_BODY_UNDER = 0.5  # overlap starts in A's main section
_P_B_BODY_UNDER = 0.8  # B's main section already running while A is still in
_P_B_SOLO = 0.4  # B's intro keeps running alone after A is out
_P_VOCAL = 6.0  # per bar of (soft) vocal clash
_P_TONAL_KEY_CLASH = 2.0  # per bar with music on both decks when the keys clash
_LENGTH_BONUS = {  # (both sides drums-only, one side, neither) — long blends only pay off when clean
    32: (6.0, -4.0, -10.0),
    16: (4.0, 2.0, -2.0),
    8: (0.0, 0.0, 0.0),
}

# order_set: weight of a poor outro→intro fit, on the scale of mixing's key/BPM costs (1 = one Camelot step)
_STRUCT_COST = 3.0
_ORDER_MAX_EVALS = 60_000
_ORDER_MAX_BLOCK = 3

_MOVE_WORDS = {
    "same": "same key",
    "+1": "+1, smooth lift",
    "-1": "-1, smooth",
    "relative": "relative major/minor",
    "+2 boost": "+2 energy boost",
    "+7 boost": "+7 energy boost",
    "diagonal": "diagonal move",
}

def _mmss(t: float) -> str:
    m, s = divmod(max(0, round(t)), 60)
    return f"{m}:{s:02d}"


def _num(x: object, default: float = 0.0) -> float:
    return float(x) if isinstance(x, (int, float)) and math.isfinite(x) else default


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# --- grid ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Grid:
    """One track's analysis on a bar grid (rescaled to the partner's bars for a half/double-time pair)."""

    fd: float  # first downbeat, s (track time)
    bar_s: float  # seconds per grid bar (track time)
    n: int
    vocal: tuple[float, ...]
    low: tuple[float, ...]
    energy: tuple[float, ...]
    perc: tuple[bool, ...]  # bar lies in a drums-only intro/outro
    intro: tuple[int, int]  # [start, end) bars
    outro: tuple[int, int]
    intro_drums: bool
    intro_vocals: bool
    outro_drums: bool
    outro_vocals: bool
    phrases: tuple[int, ...]  # bars where a phrase starts (8-bar grid + on-grid section starts)
    drops: tuple[int, ...]  # starts of drop sections
    end: int  # bar where the music ends (end of the outro)
    off: int  # phrase-grid offset: bars of pickup before the first phrase
    kick: tuple[float, ...] = ()  # kick presence per bar (empty: analysed before kick_per_bar existed)
    bass: tuple[float, ...] = ()  # how much the bass fills the gaps between the kicks, per bar (same)

    def t(self, bar: float) -> float:
        return round(self.fd + bar * self.bar_s, 3)

    def mean(self, arr: tuple[float, ...], lo: int, hi: int) -> float:
        vals = arr[max(0, lo) : max(0, hi)]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def intro_bars(self) -> int:
        return self.intro[1] - self.intro[0]

    @property
    def outro_bars(self) -> int:
        return self.outro[1] - self.outro[0]


def _grid(an: dict | None, scale: float = 1.0) -> _Grid | None:
    """Grid view of a TrackAnalysis dict, or None when it is missing or has no usable beat grid."""
    if not isinstance(an, dict):
        return None
    bar_s = _num(an.get("bar_s")) / scale
    n_src = int(_num(an.get("bar_count")))
    fd = _num(an.get("first_downbeat_s"))
    if bar_s <= 0 or not 0 < n_src <= _MAX_BARS:
        return None
    n = max(1, int(n_src * scale))

    def bar(t: object, default: int) -> int:
        if not isinstance(t, (int, float)) or not math.isfinite(t):
            return max(0, min(n, default))
        return max(0, min(n, round((t - fd) / bar_s)))

    def per_bar(key: str) -> tuple[float, ...]:
        raw = an.get(key)
        src = [_num(v) for v in raw] if isinstance(raw, list) else []
        out = []
        for k in range(n):
            lo = int(k / scale)
            vals = src[lo : max(lo + 1, int((k + 1) / scale))]
            out.append(max(vals) if vals else 0.0)
        return tuple(out)

    intro_d = an.get("intro") if isinstance(an.get("intro"), dict) else {}
    outro_d = an.get("outro") if isinstance(an.get("outro"), dict) else {}
    intro_drums, outro_drums = bool(intro_d.get("drums_only")), bool(outro_d.get("drums_only"))
    raw_sections = []
    for s in an.get("sections") if isinstance(an.get("sections"), list) else []:
        if isinstance(s, dict):
            s0, s1 = bar(s.get("start_s"), -1), bar(s.get("end_s"), -1)
            if 0 <= s0 < s1:
                raw_sections.append((str(s.get("kind") or ""), s0, s1))

    # 8-bar phrase grid from the analysis' phrase_offset; older analyses: the section starts' common bar mod 8 when
    # most agree, else bar 0. Boundaries within a bar of the grid are snapped onto it (older segmentations are +-1
    # bar noisy); section starts on the grid or half a phrase off it start phrases too, anything else is noise.
    given = an.get("phrase_offset")
    if isinstance(given, int) and not isinstance(given, bool) and 0 <= given < PHRASE:
        off = round(given * scale) % PHRASE
    else:
        offsets = [s0 % PHRASE for _, s0, _ in raw_sections if s0 > 0]
        top = min(range(PHRASE), key=lambda r: (-offsets.count(r), r))
        off = top if offsets.count(top) >= max(2, len(offsets) / 2) else 0

    def snap(k: int) -> int:
        q = off + round((k - off) / PHRASE) * PHRASE
        return q if abs(q - k) <= 1 and 0 < q < n else k

    i0 = bar(intro_d.get("start_s"), 0)
    i1 = max(i0, snap(bar(intro_d.get("end_s"), i0 + round(_num(intro_d.get("bars")) * scale))))
    end = bar(outro_d.get("end_s"), n)
    o0 = min(end, max(i1, snap(bar(outro_d.get("start_s"), end - round(_num(outro_d.get("bars")) * scale)))))
    sections = [(kind, snap(s0) if s0 else 0, snap(s1)) for kind, s0, s1 in raw_sections]
    pts = set(range(off, end + 1, PHRASE))
    for _, s0, s1 in sections:
        if s0 > 0 and (s0 - off) % (PHRASE // 2) == 0:
            pts.update(range(s0, s1, PHRASE))

    return _Grid(
        fd=fd, bar_s=bar_s, n=n,
        vocal=per_bar("vocal_per_bar"), low=per_bar("low_per_bar"), energy=per_bar("energy_per_bar"),
        perc=tuple((intro_drums and i0 <= k < i1) or (outro_drums and o0 <= k < end) for k in range(n)),
        intro=(i0, i1), outro=(o0, end),
        intro_drums=intro_drums, intro_vocals=bool(intro_d.get("has_vocals")),
        outro_drums=outro_drums, outro_vocals=bool(outro_d.get("has_vocals")),
        phrases=tuple(sorted(p for p in pts if 0 <= p <= end)),
        drops=tuple(sorted({s0 for kind, s0, _ in sections if kind == "drop"})),
        end=end, off=off,
        kick=per_bar("kick_per_bar") if isinstance(an.get("kick_per_bar"), list) else (),
        bass=per_bar("bass_per_bar") if isinstance(an.get("bass_per_bar"), list) else (),
    )  # fmt: skip


def _tempo(track: Track, an: dict | None) -> tuple[float, str | None]:
    """(BPM of the audio, warning). The analysis tempo when present (the grid the times are on), else tunebat's.
    A detected tempo unrelated to tunebat's (x1/x2/x0.5 within 3 %) means the file is probably another track."""
    tb = _num(track.bpm)
    an = an if isinstance(an, dict) else {}
    bpm = _num(an.get("bpm"))
    if bpm <= 0:
        return tb, None
    if an.get("bpm_source") == "tunebat" or tb <= 0:
        return bpm, None
    if any(abs(bpm / tb - r) / r <= _GRID_BPM_TOL for r in (0.5, 1.0, 2.0)):
        return bpm, None
    return bpm, f"tempo doesn't match tunebat ({bpm:.1f} vs {tb:.0f}): probably a wrong download"


def _pitch_on_b(a_bpm: float, b_bpm: float) -> tuple[float, float] | None:
    """(tempo change on B in %, factor) to play B (x1, x2 or x0.5) at A's tempo."""
    p = mixing._pitch(a_bpm, b_bpm)
    if p is None:
        return None
    return (a_bpm / (b_bpm * p[1]) - 1) * 100, p[1]


# --- planning ------------------------------------------------------------------------------------------------------


@dataclass
class _Plan:
    technique: str
    a0: int  # A bar where B comes in (the cut point for cut-style techniques)
    b0: int  # B's cue bar
    length: int  # overlap in bars (0 = cut on the 1)
    fit: float  # structural fit, 0-100 after clamping
    clash: list[int]  # overlap offsets with vocals on both decks
    tonal: int = 0  # overlap bars with music (not just drums) on both decks


def _window(a: _Grid, b: _Grid, a0: int, b0: int, length: int, key_clash: bool) -> _Plan:
    """Score one phrase-aligned overlap: A bars [a0, a0+L) under B bars [b0, b0+L)."""
    e = a0 + length
    o0, end = a.outro
    i0, i1 = b.intro
    pen = _P_BODY_CUT * max(0, o0 - e)
    pen += _P_OUTRO_SKIP * (end - max(e, o0))
    pen += _P_INTRO_SKIP * (b0 - i0)
    pen += _P_A_BODY_UNDER * max(0, min(o0, e) - a0)
    pen += _P_B_BODY_UNDER * max(0, b0 + length - max(i1, b0))
    pen += _P_B_SOLO * max(0, i1 - (b0 + length))

    clash, soft, tonal = [], 0.0, 0
    for k in range(length):
        va, vb = a.vocal[a0 + k], b.vocal[b0 + k]
        soft += max(0.0, min(va, vb) - _VOCAL_FLOOR) / (1 - _VOCAL_FLOOR)
        if va >= _VOCAL_ON and vb >= _VOCAL_ON:
            clash.append(k)
        tonal += not (a.perc[a0 + k] or b.perc[b0 + k])
    pen += _P_VOCAL * soft + (_P_TONAL_KEY_CLASH * tonal if key_clash else 0.0)

    clean_a = a.outro_drums and a0 >= o0
    clean_b = b.intro_drums and b0 + length <= i1
    both, one, neither = _LENGTH_BONUS[length]
    bonus = both if clean_a and clean_b else one if clean_a or clean_b else neither
    return _Plan("phrase blend", a0, b0, length, 100.0 - pen + bonus, clash, tonal)


def _best_blend(a: _Grid, b: _Grid, key_clash: bool) -> _Plan | None:
    """Best phrase-aligned overlap of A's ending with B's beginning (ties keep the earlier, longer candidate)."""
    i0, i1 = b.intro
    b_cues = [p for p in b.phrases if p <= max(b.phrases[0], i1 - PHRASE)] if b.phrases else [i0]
    best: _Plan | None = None
    for length in _LENGTHS:
        for a0 in a.phrases:
            if a0 < a.end // 2 or a0 + length > a.end or a0 + length < a.end - _MAX_CUT_BARS:
                continue
            for b0 in b_cues:
                if b0 + length <= b.n:
                    plan = _window(a, b, a0, b0, length, key_clash)
                    if best is None or plan.fit > best.fit + 1e-9:
                        best = plan
    return best


def _first_cue(b: _Grid) -> int:
    """B's first phrase start (bar 0 unless it has a pickup)."""
    return b.phrases[0] if b.phrases else b.intro[0]


def _abrupt(a: _Grid) -> bool:
    """A stops cold: still at full power within its last few bars (no energy data: under 4 bars of outro).

    A real outro of a phrase or more (drums + bass with vocals/leads gone) is loud by nature, so it never counts.
    """
    if not any(a.energy):
        return a.outro_bars < PHRASE // 2
    if a.outro_bars >= PHRASE:
        return False
    loud = [k for k in range(max(0, a.end - PHRASE), a.end) if a.energy[k] >= _LOUD]
    return bool(loud) and a.end - 1 - loud[-1] <= _COLD_END_BARS


def _cut_bar(a: _Grid) -> int:
    """Where to leave A without a blend: its last phrase start, unless that throws away more than 4 bars."""
    p = max((p for p in a.phrases if p <= a.end), default=a.end)
    return p if a.end - p <= PHRASE // 2 else a.end


def _plan(a: _Grid, b: _Grid, key_clash: bool) -> _Plan:
    """Pick the technique and its exact bars for A → B."""
    blend = _best_blend(a, b, key_clash)
    if blend is not None and (
        blend.fit < _MIN_BLEND_FIT
        or len(blend.clash) > _MAX_CLASH_BARS
        or (key_clash and blend.tonal > _MAX_TONAL_CLASH_BARS)
    ):
        blend = None

    if _abrupt(a) and not _CHILL:  # chill mixes have no echo: a cold ending is just where the blend ends
        if blend is None:
            return _Plan("echo out", _cut_bar(a), _first_cue(b), 0, _CUT_FIT["echo out"], [])
        if a.end - (blend.a0 + blend.length) < _MIN_SKIP_NOTE:  # the blend runs into A's cold ending
            blend.technique = "echo out"
            return blend
    if blend is not None:
        a0, b0, length = blend.a0, blend.b0, blend.length
        if length >= 16 and a.outro_drums and a0 >= a.outro[0] and b.intro_drums and b0 + length <= b.intro[1]:
            blend.technique = "long blend"
        elif a.mean(a.low, a0, a0 + length) >= _BASS_ON and b.mean(b.low, b0, b0 + length) >= _BASS_ON:
            blend.technique = "bass swap"
        return blend

    a_drops = [d for d in a.drops if a.end * 0.4 <= d <= a.end - PHRASE]
    b_drops = [d for d in b.drops if d > b.intro[0]]
    if a_drops and b_drops:
        return _Plan("drop swap", a_drops[-1], b_drops[0], 0, _CUT_FIT["drop swap"], [])
    return _Plan("quick cut", _cut_bar(a), _first_cue(b), 0, _CUT_FIT["quick cut"], [])


def _swap_offset(b: _Grid, plan: _Plan) -> int | None:
    """0-based overlap bar where B's low end replaces A's: where B's main section arrives if that's inside the
    overlap on a phrase, else halfway; None for cuts and when B brings no bass during the overlap."""
    b0, length = plan.b0, plan.length
    if not length or b.mean(b.low, b0, b0 + length) < _NO_BASS:
        return None
    k = b.intro[1] - b0
    return k if 0 < k < length and k % PHRASE == 0 else length // 2


# --- wording -------------------------------------------------------------------------------------------------------


def _edge_clause(g: _Grid, which: str, who: str) -> str:
    """'A's 16-bar outro is drums-only', 'B starts full-on (no intro)', ..."""
    lo, hi = g.intro if which == "intro" else g.outro
    drums, vocals = (g.intro_drums, g.intro_vocals) if which == "intro" else (g.outro_drums, g.outro_vocals)
    if hi <= lo:
        verb = "starts" if which == "intro" else "ends"
        extra = " with vocals" if vocals else " on drums" if drums else ""
        return f"{who} {verb} full-on{extra} (no {which})"
    edge = f"{who}'s {hi - lo}-bar {which}"
    if drums:
        return f"{edge} is drums-only"
    if vocals:
        return f"{edge} has vocals"
    return f"{edge} carries {'bass and music' if g.mean(g.low, lo, hi) >= _BASS_ON else 'music'}"


def _headline(a: _Grid, b: _Grid, plan: _Plan) -> str:
    at = _mmss(a.t(plan.a0))
    b_edge = _edge_clause(b, "intro", "B")
    if plan.technique == "echo out":
        edges = f"A stops cold (full power to the end); {b_edge}"
        if plan.length:
            return f"{edges} → {plan.length}-bar blend from A {at}, then echo A out on its last beat."
        return f"{edges} → echo out A at {at}, B in on the 1."
    if a.outro_drums and a.outro_bars and b.intro_drums and b.intro_bars:
        b_edge = f"B's {b.intro_bars}-bar intro too"
    if plan.length:
        what = f"{plan.length}-bar {plan.technique} starting at A {at}"
    elif plan.technique == "drop swap":
        what = f"drop swap at A {at} into B's drop at {_mmss(b.t(plan.b0))}"
    else:
        what = f"quick cut on the 1 at A {at}"
    return f"{_edge_clause(a, 'outro', 'A')}; {b_edge} → {what}."


def _cue_words(b: _Grid, b0: int) -> str:
    i0, i1 = b.intro
    if b0 == i0:
        return "its first downbeat" if b0 == 0 else f"the start of its intro, bar {b0 + 1}"
    if b.drops and b0 == b.drops[0]:
        return f"its first drop, bar {b0 + 1}"
    if i0 < b0 < i1:
        return f"bar {b0 + 1}, {_plural(b0 - i0, 'bar')} into its intro"
    return f"bar {b0 + 1}"


def _plan_notes(a: _Grid, b: _Grid, plan: _Plan) -> list[str]:
    a0, b0, length = plan.a0, plan.b0, plan.length
    t_in, t_b = _mmss(a.t(a0)), _mmss(b.t(b0))
    notes = [_headline(a, b, plan)]
    if plan.technique == "drop swap":
        notes.append(
            f"Cue B at {t_b} ({_cue_words(b, b0)}). Let A's build run and on the 1 where A would drop "
            f"(A {t_in}, bar {a0 + 1}) hard-cut to B."
        )
        notes.append(f"Skips A's last {a.end - a0} bars and B's first {_plural(b0, 'bar')}: a quick-mixing move.")
        return notes
    if not length:
        lead = "Hit an echo/delay on A's last beat and drop" if plan.technique == "echo out" else "Cut to"
        where = f"bar {a0 + 1}" if a0 < a.end else "its end"
        notes.append(f"Cue B at {t_b} ({_cue_words(b, b0)}). {lead} B on the 1 at A {t_in} ({where}).")
        if a.end - a0 >= _MIN_SKIP_NOTE:
            notes.append(f"Skips A's last {a.end - a0} bars ({t_in}–{_mmss(a.t(a.end))}).")
        return notes

    e, i1 = a0 + length, b.intro[1]
    notes.append(f"Cue B at {t_b} ({_cue_words(b, b0)}) and start it at A {t_in} (A bar {a0 + 1}, on the phrase).")
    if b0 == b.off > 0 and a0 >= b.off:
        notes.append(
            f"B has a {b.off}-bar pickup before its first phrase: you can also play it from its first downbeat, "
            f"starting {b.off} bars earlier at A {_mmss(a.t(a0 - b.off))}."
        )
    k = _swap_offset(b, plan)
    if k is None:
        notes.append("B brings no bass in this stretch: let A's bass carry the blend, then cut it on the 1.")
    else:
        notes.append(
            f"Keep B's lows cut, swap the bass on the 1 at A {_mmss(a.t(a0 + k))} "
            f"(bar {k + 1} of the blend, B {_mmss(b.t(b0 + k))})."
        )
    if plan.technique == "echo out":
        notes.append(f"Hit an echo/delay on A's last beat at {_mmss(a.t(e))} so its cold stop isn't heard.")
    else:
        when = f"its end ({_mmss(a.t(e))})" if e >= a.end else f"A {_mmss(a.t(e))} (bar {e + 1})"
        notes.append(f"Have A faded out by {when}.")
    b_end = b0 + length
    if b_end == i1:
        notes.append(f"B's intro ends exactly as A leaves → B's main section takes over at B {_mmss(b.t(i1))}.")
    elif b_end < i1:
        notes.append(f"B's intro runs {_plural(i1 - b_end, 'more bar')} on its own after A is out.")
    else:
        start = max(i1, b0)
        bass = ": get A's bass out by then" if b.mean(b.low, start, b_end) >= _BASS_ON else ""
        notes.append(f"B's main section starts {b_end - start} bars before A is out (B {_mmss(b.t(start))}){bass}.")
    if a.end - e >= _MIN_SKIP_NOTE:
        notes.append(f"Skips A's last {a.end - e} bars ({_mmss(a.t(e))}–{_mmss(a.t(a.end))}).")
    return notes


def _plan_warnings(a: _Grid, b: _Grid, plan: _Plan) -> list[str]:
    warnings = []
    if plan.clash:
        k0, k1 = plan.clash[0], plan.clash[-1] + 1
        warnings.append(
            f"Vocals overlap for {_plural(len(plan.clash), 'bar')} "
            f"(A {_mmss(a.t(plan.a0 + k0))}–{_mmss(a.t(plan.a0 + k1))}): ride the mids or shorten the blend."
        )
    if not plan.length and plan.technique != "echo out":
        if a.outro_vocals and b.intro_vocals:
            warnings.append("A's outro and B's intro both have vocals: no clean blend, so cut.")
        elif b.intro_bars < PHRASE and a.outro_bars < PHRASE:
            warnings.append(f"Short edges (A outro {a.outro_bars}, B intro {b.intro_bars} bars): no room to blend.")
    if plan.technique != "drop swap" and not b.intro_bars:
        warnings.append("B has no intro: it starts full-on.")
    elif plan.technique != "drop swap" and b.intro_bars < PHRASE:
        warnings.append(f"B's intro is only {_plural(b.intro_bars, 'bar')}: its main section hits almost at once.")
    elif plan.length > b.intro_bars and not (a.outro_drums and plan.a0 >= a.outro[0]):
        warnings.append(
            f"B's intro ({b.intro_bars} bars) is shorter than the {plan.length}-bar blend, "
            "its main section comes in over A's music."
        )
    if plan.technique not in ("echo out", "drop swap") and a.outro_bars < PHRASE:
        warnings.append(f"A's outro is only {_plural(a.outro_bars, 'bar')}." if a.outro_bars else "A has no outro.")
    return warnings


def _key_text(a: Track, b: Track, move: str, plan: _Plan | None) -> tuple[list[str], list[str]]:
    key = f"{a.camelot or '?'} → {b.camelot or '?'}"
    if move == "?":
        return [], [f"Key unknown ({key})."]
    if move != "clash":
        return [f"Key {key}: {_MOVE_WORDS.get(move, move)}."], []
    if plan is not None and plan.length and plan.tonal == 0:
        return [f"Key {key} clashes, but the overlap is drums-only on one deck at all times, so it should pass."], []
    if plan is not None and not plan.length:
        return [], [f"Key clash {key}: cut, don't blend."]
    return [], [f"Key clash {key}: keep the overlap short and percussive."]


def _tempo_text(
    a_bpm: float, b_bpm: float, pitch: tuple[float, float] | None, tol: float
) -> tuple[list[str], list[str]]:
    if pitch is None:
        return [], ["BPM unknown: beatmatch by ear."]
    pct, factor = pitch
    notes, warnings = [], []
    if factor != 1 and abs(pct) <= tol * 100:
        kind = "half" if factor == 2 else "double"
        notes.append(f"B is {kind}-time ({b_bpm:.0f} BPM): mix it at {b_bpm * factor:.0f}.")
    if abs(pct) < 0.05:
        notes.append(f"Same tempo ({a_bpm:.1f} BPM).")
    else:
        notes.append(f"Pitch B {pct:+.1f}% ({b_bpm * factor:.1f} → {a_bpm:.1f} BPM).")
    if abs(pct) > tol * 100:
        warnings.append(f"B needs {pct:+.1f}% pitch, outside the ±{tol * 100:.0f}% tempo range.")
    elif abs(pct) > _BIG_PITCH:
        warnings.append(f"Big tempo change ({pct:+.1f}% on B): use key lock.")
    return notes, warnings


# --- public API ----------------------------------------------------------------------------------------------------


def _suggest(
    a: Track, a_an: dict | None, b: Track, b_an: dict | None, bpm_tol: float, cache: dict | None
) -> tuple[dict, float]:
    """(Transition dict, structural fit 0-100) — the fit also feeds order_set."""
    move = mixing._camelot_move(a.camelot, b.camelot)
    (a_bpm, a_wrong), (b_bpm, b_wrong) = _tempo(a, a_an), _tempo(b, b_an)
    pitch = _pitch_on_b(a_bpm, b_bpm)

    def grid(t: Track, an: dict | None, scale: float) -> _Grid | None:
        if cache is None:
            return _grid(an, scale)
        k = (t.id, scale)
        if k not in cache:
            cache[k] = _grid(an, scale)
        return cache[k]

    ga, gb = grid(a, a_an, 1.0), grid(b, b_an, pitch[1] if pitch else 1.0)
    tempo_notes, tempo_warnings = _tempo_text(a_bpm, b_bpm, pitch, bpm_tol)
    wrong = [f"{name}'s {w}." for name, w in (("A", a_wrong), ("B", b_wrong)) if w]
    out: dict = {
        "from_id": a.id, "to_id": b.id, "score": 0, "key_move": move,
        "pitch_pct": round(pitch[0], 2) if pitch else 0.0,
        "out_start_s": None, "in_start_s": None, "overlap_bars": None, "swap_bar": None, "technique": None,
        "notes": [], "warnings": [], "ready": False,
    }  # fmt: skip
    if ga is None or gb is None:
        fit = _NEUTRAL_FIT
        key_notes, key_warnings = _key_text(a, b, move, None)
        missing = [name for name, g in (("A", ga), ("B", gb)) if g is None]
        wait = f"Mix points appear once {' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} analysed."
        out["notes"] = [*key_notes, *tempo_notes, wait]
        out["warnings"] = [*wrong, *key_warnings, *tempo_warnings]
    else:
        plan = _plan(ga, gb, key_clash=move == "clash")
        fit = max(0.0, min(100.0, plan.fit - _WRONG_FILE_FIT * len(wrong)))
        key_notes, key_warnings = _key_text(a, b, move, plan)
        out_s, in_s = ga.t(plan.a0), gb.t(plan.b0)
        if in_s < 0:  # B's grid starts before its file (clipped first beat): start it from 0, that much later
            out_s, in_s = round(out_s - in_s * ga.bar_s / gb.bar_s, 3), 0.0
        swap = _swap_offset(gb, plan)
        out.update(
            out_start_s=out_s, in_start_s=in_s, overlap_bars=plan.length,
            swap_bar=None if swap is None else swap + 1, technique=plan.technique, ready=True,
        )  # fmt: skip
        out["notes"] = [*_plan_notes(ga, gb, plan), *tempo_notes, *key_notes]
        out["warnings"] = [*wrong, *_plan_warnings(ga, gb, plan), *key_warnings, *tempo_warnings]
    harmonic = mixing.score(a, b, tol=bpm_tol).value
    out["score"] = max(0, min(100, round(_HARMONIC_WEIGHT * harmonic + (1 - _HARMONIC_WEIGHT) * fit)))
    return out, fit


def suggest(a: Track, a_an: dict | None, b: Track, b_an: dict | None, bpm_tol: float = 0.10) -> dict:
    """Transition dict (types.ts) for playing B after A; ready=False (key/BPM only) until both analyses exist."""
    return _suggest(a, a_an, b, b_an, bpm_tol, None)[0]


def suggest_set(tracks: list[Track], analyses: dict[str, dict | None], bpm_tol: float = 0.10) -> list[dict]:
    """Transitions for each consecutive pair: tracks[i] → tracks[i+1]."""
    cache: dict = {}
    return [
        _suggest(a, analyses.get(a.id), b, analyses.get(b.id), bpm_tol, cache)[0] for a, b in zip(tracks, tracks[1:])
    ]


def track_dict(t: Track) -> dict:
    """Track in the wire format of types.ts."""
    return {
        "id": t.id, "name": t.name, "artists": list(t.artists), "display_name": t.display_name, "album": t.album,
        "release_date": t.release_date, "duration_s": t.duration_s, "popularity": t.popularity, "key": t.key,
        "camelot": t.camelot, "bpm": t.bpm, "energy": t.energy, "danceability": t.danceability,
        "cover_url": t.cover_url, "spotify_url": t.spotify_url,
    }  # fmt: skip


def partners(
    track: Track,
    analysis: dict | None,
    candidates: list[tuple[Track, dict | None]],
    direction: str = "out",
    limit: int = 8,
    bpm_tol: float = 0.10,
) -> list[dict]:
    """Best tracks to play after `track` (direction "out") or before it ("in"), as Partner dicts, best first."""
    if direction not in ("out", "in"):
        raise ValueError(f"direction must be 'out' or 'in', not {direction!r}")
    cache: dict = {}
    ranked = []
    seen = {track.id}
    for other, an in candidates:
        if other.id in seen:
            continue
        seen.add(other.id)
        if direction == "out":
            tr = _suggest(track, analysis, other, an, bpm_tol, cache)[0]
        else:
            tr = _suggest(other, an, track, analysis, bpm_tol, cache)[0]
        ranked.append(((-tr["score"], not tr["ready"], other.display_name.lower(), other.id), other, tr))
    ranked.sort(key=lambda r: r[0])
    return [{"track": track_dict(t), "transition": tr, "direction": direction} for _, t, tr in ranked[: max(0, limit)]]


def order_set(
    seed: Track, tracks: list[Track], analyses: dict[str, dict | None], bpm_tol: float = 0.10
) -> list[Track]:
    """mixing.order_set refined with outro→intro fit: seed first, every track once, deterministic.

    With fewer than two analyses this is exactly mixing.order_set. Otherwise it minimises mixing's key/tempo/energy
    cost plus a penalty for pairs whose ending and beginning don't mix well, by local search from mixing's order.
    """
    base = mixing.order_set(seed, tracks)
    n = len(base)
    if n < 3 or sum(_grid(analyses.get(t.id)) is not None for t in base) < 2:
        return base

    cache: dict = {}
    struct: list[list[float | None]] = [[None] * n for _ in range(n)]
    for i, a in enumerate(base):
        for j, b in enumerate(base):
            if i != j and j and analyses.get(a.id) is not None and analyses.get(b.id) is not None:
                fit = _suggest(a, analyses[a.id], b, analyses[b.id], bpm_tol, cache)[1]
                struct[i][j] = _STRUCT_COST * (1 - fit / 100)
    known = sorted(c for row in struct for c in row if c is not None)
    neutral = known[len(known) // 2] if known else 0.0

    levels = mixing._energy_levels(base)
    raw_en = [levels[t.id] for t in base]
    lo, hi = min(raw_en), max(raw_en)
    en = [(v - lo) / (hi - lo) if hi - lo > mixing._EPS else 0.5 for v in raw_en]
    start = max(0.2, min(0.6, en[0]))
    target = [mixing._energy_target(p / (n - 1), start) for p in range(n)]
    shape = [[mixing._SHAPE_COST * abs(en[j] - target[p]) for p in range(n)] for j in range(n)]
    step = [
        [
            mixing._pair_cost(base[i], base[j])
            + mixing._JUMP_COST * abs(en[i] - en[j])
            + (neutral if struct[i][j] is None else struct[i][j])
            for j in range(n)
        ]
        for i in range(n)
    ]

    def cost(order: list[int]) -> float:
        return sum(step[x][y] for x, y in zip(order, order[1:])) + sum(shape[j][p] for p, j in enumerate(order) if p)

    def moves(order: list[int]) -> Iterator[list[int]]:
        """Swap two tracks, or move a block of 1-3 tracks (either way round) elsewhere; the seed stays first."""
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                yield order[:i] + [order[j]] + order[i + 1 : j] + [order[i]] + order[j + 1 :]
        for size in range(1, _ORDER_MAX_BLOCK + 1):
            for i in range(1, n - size + 1):
                block, rest = order[i : i + size], order[:i] + order[i + size :]
                for k in range(1, len(rest) + 1):
                    if k != i:
                        yield rest[:k] + block + rest[k:]
                        if size > 1:
                            yield rest[:k] + block[::-1] + rest[k:]

    best = list(range(n))
    best_cost, evals, improved = cost(best), 0, True
    while improved and evals < _ORDER_MAX_EVALS:
        improved = False
        for cand in moves(best):
            evals += 1
            c = cost(cand)
            if c < best_cost - 1e-9:
                best, best_cost, improved = cand, c, True
                break
            if evals >= _ORDER_MAX_EVALS:
                break
    return [base[i] for i in best]
