"""Transition styles as data (#57): how two tracks are mixed, by genre, in `seedbox2/mix/styles/*.toml`.

A style is one file (its name is the style's id). It says when the style applies, which of Seedbox 1's techniques
places its window (`base`: the generator's cue points, tempo map and loops), and how it changes what that generator
wrote: lane templates, stem lanes, sends, deck effects and how far each deck plays. `TECHNIQUES` is every style's
technique, in file order: the one list the planner, the renderer (`render_technique`), the API (`GET
/api/mixes/styles`) and the UI (`frontend2/src/api/mixplan.generated.ts`) read.

```toml
technique = "double drop"   # the plan's technique
label = "Double drop"
kind = "swap"               # its colour: blend, swap, filter, echo or cut
hint = "Both drops land together; B's bass takes over on the drop."
base = "slam"               # the Seedbox 1 technique that places the window
weight = 4.0                # rank among the styles that apply (the generator's own pick has AUTO_BONUS more)

[when]                      # every key is optional
genres = ["drum_and_bass"]  # A's genre family (genres.toml) and B's, when known; missing: any genre, or none known
bpm = [160, 185]            # A's BPM, or twice it (half-time grids)
energy_a = [0.6, 1.0]       # the tracks' energy, 0-1
energy_b = [0.6, 1.0]
vocals = ["none", "a"]      # who sings at the transition: none, a (A's end), b (B's start) or both
tempo_gap = [0.0, 0.06]     # |B / A - 1|, after half or double time
replaces = ["slam"]         # only instead of these picks of the generator
needs = ["drops"]           # drops (A's and B's on the cut), b_stems, a_stems, b_acapella

[boost]                     # extra weight per genre family
house = 1.0

[length]
bars = 16                   # what to ask the generator for; missing: its own choice
span = [-8, 16]             # bars around beat 0 the transition covers; missing: the base's
swap = 0.5                  # where "swap" is, as a share of the span

[lanes.a]                   # replaces a lane of deck a: [where, value] or [where, value, curve]
volume = [["swap", 1.0, "ease"], ["end", 0.0]]
[stems.b]                   # the same for a stem lane (a deck without stems plays it on its EQ)
gain_vocals = [["start", 1.0]]
[sends.a]                   # echo_beats, reverb_size
echo_beats = 0.75
[[fx.a]]                    # a deck effect: brake, spinback or stutter (with div)
type = "brake"
start = "end-2b"
end = "end"
```

`where` is `start`, `end`, `swap`, `cut` (beat 0) or a share of the span (0-1), and may add beats or bars: `"end-2b"`,
`"start+1bar"`, `"0.5+4b"`. Values are the plan's units (dB on the EQ, linear gain on the fader and stems).
"""

from __future__ import annotations

import functools
import math
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..plan import (
    LANE_DEFAULTS,
    LANES,
    STEM_LANES,
    Keyframe,
    MixPlan,
    deck_grid,
    validate,
)  # fmt: skip

FOLDER = Path(__file__).resolve().parent
KINDS = ("blend", "swap", "filter", "echo", "cut")
VOCAL_STATES = ("none", "a", "b", "both")
NEEDS = ("drops", "a_stems", "b_stems", "b_acapella")
SEED1_TECHNIQUES = (
    "long blend", "phrase blend", "bass swap", "drop swap", "quick cut", "echo out", "filter sweep", "loop roll", "slam",
    "reverb wash", "spinback", "brake", "stutter",
)  # musicreco.mixplan.TECHNIQUES, without importing the generator for a list  # fmt: skip
FX_TYPES = ("spinback", "brake", "stutter")
CURVES = ("linear", "hold", "ease")
AUTO_BONUS = 2.0  # the generator's own pick knows the window fits: a genre style has to beat it by data
MAX_TEMPO_GAP = 0.06  # musicreco.mixplan.MAX_BLEND_GAP: a blend style without its own gap limit keeps to this
DROP_SNAP_BEATS = 1.0  # a drop this close to beat 0 is on the cut
LEAD_BEATS = 8.0  # the window keeps this much of each deck alone either side of the span
SEND_TAIL = 4.0  # beats A plays on (fader down) after the span when it sends to its echo or reverb
_WHERE = re.compile(r"^\s*(start|end|swap|cut|[0-9]*\.?[0-9]+)\s*(?:([+-])\s*([0-9]*\.?[0-9]+)\s*(b|bar|bars)?)?\s*$")


class StyleError(ValueError):
    """A style file that can't be used."""


@dataclass(frozen=True)
class Point:
    where: str
    value: float
    curve: str = "linear"


@dataclass(frozen=True)
class Fx:
    type: str
    start: str
    end: str
    div: float | None = None


@dataclass(frozen=True)
class When:
    genres: tuple[str, ...] = ()
    bpm: tuple[float, float] | None = None
    energy_a: tuple[float, float] | None = None
    energy_b: tuple[float, float] | None = None
    vocals: tuple[str, ...] = ()
    tempo_gap: tuple[float, float] | None = None
    replaces: tuple[str, ...] = ()
    needs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Style:
    id: str
    technique: str
    label: str
    kind: str
    hint: str
    base: str
    weight: float
    when: When
    boost: dict[str, float] = field(default_factory=dict)
    bars: int | None = None
    span: tuple[float, float] | None = None
    swap: float = 0.5
    lanes: dict[str, dict[str, tuple[Point, ...]]] = field(default_factory=dict)
    stems: dict[str, dict[str, tuple[Point, ...]]] = field(default_factory=dict)
    sends: dict[str, dict[str, float]] = field(default_factory=dict)
    fx: dict[str, tuple[Fx, ...]] = field(default_factory=dict)
    loops: bool = True  # keep the base's loops (false: the style plays both decks straight)

    @property
    def is_port(self) -> bool:
        """A Seedbox 1 technique as its generator writes it."""
        return self.technique == self.base and self.loops and not (self.lanes or self.stems or self.sends or self.fx or self.span)

    def to_json(self) -> dict:
        w = self.when
        return {
            "id": self.id, "technique": self.technique, "label": self.label, "kind": self.kind, "hint": self.hint,
            "base": self.base, "weight": self.weight, "boost": self.boost, "bars": self.bars,
            "when": {"genres": list(w.genres), "bpm": w.bpm, "energy_a": w.energy_a, "energy_b": w.energy_b,
                     "vocals": list(w.vocals), "tempo_gap": w.tempo_gap, "replaces": list(w.replaces),
                     "needs": list(w.needs)},
        }  # fmt: skip


# --- reading the files -----------------------------------------------------------------------------------------------


def _range(raw: Any, what: str) -> tuple[float, float] | None:
    if raw is None:
        return None
    if not (isinstance(raw, list) and len(raw) == 2 and all(isinstance(x, (int, float)) for x in raw) and raw[0] <= raw[1]):
        raise StyleError(f"{what} must be [low, high]")
    return float(raw[0]), float(raw[1])


def _strings(raw: Any, what: str, allowed: tuple[str, ...] | None = None) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not (isinstance(raw, list) and all(isinstance(x, str) for x in raw)):
        raise StyleError(f"{what} must be a list of strings")
    bad = [x for x in raw if allowed is not None and x not in allowed]
    if bad:
        raise StyleError(f"{what}: unknown {bad} (one of {list(allowed or ())})")
    return tuple(raw)


def _where(raw: Any, what: str) -> str:
    text = str(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else raw
    if not isinstance(text, str) or not _WHERE.match(text):
        raise StyleError(f"{what}: {raw!r} isn't start, end, swap, cut or a share of the span, plus beats or bars")
    return text


def _points(raw: Any, what: str) -> tuple[Point, ...]:
    if not isinstance(raw, list) or not raw:
        raise StyleError(f"{what} must be a list of [where, value] or [where, value, curve]")
    out = []
    for i, p in enumerate(raw):
        if not (isinstance(p, list) and len(p) in (2, 3) and isinstance(p[1], (int, float))):
            raise StyleError(f"{what}[{i}] must be [where, value] or [where, value, curve]")
        curve = p[2] if len(p) == 3 else "linear"
        if curve not in CURVES:
            raise StyleError(f"{what}[{i}]: curve {curve!r} isn't one of {list(CURVES)}")
        out.append(Point(_where(p[0], f"{what}[{i}]"), float(p[1]), curve))
    return tuple(out)


def _decks(raw: Any, names: tuple[str, ...], what: str) -> dict[str, dict[str, tuple[Point, ...]]]:
    out: dict[str, dict[str, tuple[Point, ...]]] = {}
    for deck, lanes in (raw or {}).items():
        if deck not in ("a", "b") or not isinstance(lanes, dict):
            raise StyleError(f"{what}: decks are a and b")
        for name, points in lanes.items():
            if name not in names:
                raise StyleError(f"{what}.{deck}: no lane {name!r}")
            out.setdefault(deck, {})[name] = _points(points, f"{what}.{deck}.{name}")
    return out


def parse(raw: dict, style_id: str) -> Style:
    """A style from a file's TOML. StyleError says what's wrong with it."""
    known = {"technique", "label", "kind", "hint", "base", "weight", "loops", "when", "boost", "length", "lanes", "stems",
             "sends", "fx"}  # fmt: skip
    extra = set(raw) - known
    if extra:
        raise StyleError(f"unknown keys {sorted(extra)}")
    for key in ("technique", "label", "kind", "base"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise StyleError(f"{key} is required")
    if raw["kind"] not in KINDS:
        raise StyleError(f"kind {raw['kind']!r} isn't one of {list(KINDS)}")
    if raw["base"] not in SEED1_TECHNIQUES:
        raise StyleError(f"base {raw['base']!r} isn't a Seedbox 1 technique")
    w = raw.get("when") or {}
    extra = set(w) - set(When.__dataclass_fields__)
    if extra:
        raise StyleError(f"when: unknown keys {sorted(extra)}")
    when = When(
        genres=_strings(w.get("genres"), "when.genres"), bpm=_range(w.get("bpm"), "when.bpm"),
        energy_a=_range(w.get("energy_a"), "when.energy_a"), energy_b=_range(w.get("energy_b"), "when.energy_b"),
        vocals=_strings(w.get("vocals"), "when.vocals", VOCAL_STATES), tempo_gap=_range(w.get("tempo_gap"), "when.tempo_gap"),
        replaces=_strings(w.get("replaces"), "when.replaces", SEED1_TECHNIQUES), needs=_strings(w.get("needs"), "when.needs", NEEDS),
    )  # fmt: skip
    length = raw.get("length") or {}
    bars = length.get("bars")
    if bars is not None and bars not in (4, 8, 16, 32):
        raise StyleError("length.bars must be 4, 8, 16 or 32")
    swap = float(length.get("swap", 0.5))
    if not 0.0 <= swap <= 1.0:
        raise StyleError("length.swap is a share of the span, 0-1")
    sends: dict[str, dict[str, float]] = {}
    for deck, values in (raw.get("sends") or {}).items():
        if deck not in ("a", "b") or not isinstance(values, dict) or set(values) - {"echo_beats", "reverb_size"}:
            raise StyleError("sends: [sends.a] or [sends.b] with echo_beats and reverb_size")
        sends[deck] = {k: float(v) for k, v in values.items()}
    fx: dict[str, tuple[Fx, ...]] = {}
    for deck, events in (raw.get("fx") or {}).items():
        if deck not in ("a", "b") or not isinstance(events, list):
            raise StyleError("fx: [[fx.a]] or [[fx.b]] tables")
        out = []
        for e in events:
            if e.get("type") not in FX_TYPES:
                raise StyleError(f"fx.{deck}: type must be one of {list(FX_TYPES)}")
            out.append(Fx(e["type"], _where(e.get("start"), f"fx.{deck}.start"), _where(e.get("end"), f"fx.{deck}.end"),
                          float(e["div"]) if "div" in e else None))  # fmt: skip
        fx[deck] = tuple(out)
    boost = raw.get("boost") or {}
    if not all(isinstance(v, (int, float)) for v in boost.values()):
        raise StyleError("boost: genre = number")
    return Style(
        id=style_id, technique=raw["technique"], label=raw["label"], kind=raw["kind"], hint=raw.get("hint", ""),
        base=raw["base"], weight=float(raw.get("weight", 1.0)), when=when, boost={k: float(v) for k, v in boost.items()},
        bars=bars, span=_range(length.get("span"), "length.span"), swap=swap,
        lanes=_decks(raw.get("lanes"), LANES, "lanes"), stems=_decks(raw.get("stems"), STEM_LANES, "stems"),
        sends=sends, fx=fx, loops=bool(raw.get("loops", True)),
    )  # fmt: skip


def load_folder(folder: Path) -> tuple[Style, ...]:
    """Every style in the folder, ordered by weight (heaviest first), then by name. StyleError names the bad file."""
    out = []
    for path in sorted(folder.glob("*.toml")):
        if path.stem == "genres":
            continue
        try:
            out.append(parse(tomllib.loads(path.read_text("utf-8")), path.stem))
        except (StyleError, tomllib.TOMLDecodeError) as e:
            raise StyleError(f"{path.name}: {e}") from e
    seen: dict[str, str] = {}
    for s in out:
        if s.technique in seen:
            raise StyleError(f"{s.id}.toml: technique {s.technique!r} is {seen[s.technique]}.toml's too")
        seen[s.technique] = s.id
    missing = set(SEED1_TECHNIQUES) - set(seen)
    if missing:
        raise StyleError(f"no style ports {sorted(missing)}")
    return tuple(sorted(out, key=lambda s: (-s.weight, s.id)))


@functools.cache
def all_styles() -> tuple[Style, ...]:
    return load_folder(FOLDER)


def by_technique(technique: str) -> Style | None:
    return next((s for s in all_styles() if s.technique == technique), None)


def by_id(style_id: str) -> Style | None:
    return next((s for s in all_styles() if s.id == style_id), None)


def techniques() -> tuple[str, ...]:
    """Every technique a plan may name: Seedbox 1's first, in its order, then the genre styles' by weight."""
    return (*SEED1_TECHNIQUES, *(s.technique for s in all_styles() if s.technique not in SEED1_TECHNIQUES))


TECHNIQUES = techniques()


def kind_of(technique: str) -> str:
    style = by_technique(technique)
    return style.kind if style else "blend"


def render_technique(technique: str) -> str:
    """What Seedbox 1's renderer is told (it only knows its own): the style's base. Lanes carry the sound, so this is a
    label; an unknown technique (an edited plan's own) renders as a long blend."""
    if technique in SEED1_TECHNIQUES:
        return technique
    style = by_technique(technique)
    return style.base if style else "long blend"


# --- when a style applies ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Context:
    """What the planner knows of the pair before it plans: genre families (None: unknown), A's BPM, the tempo gap,
    energies 0-1 (None: unknown), who sings at the transition, stems, and the generator's own pick."""

    genre_a: str | None = None
    genre_b: str | None = None
    bpm: float | None = None
    tempo_gap: float = 0.0
    energy_a: float | None = None
    energy_b: float | None = None
    vocals: str = "none"
    a_stems: bool = False
    b_stems: bool = False
    b_acapella: bool = False
    auto: str | None = None
    a_duration_s: float | None = None
    b_duration_s: float | None = None
    a_drops_s: tuple[float, ...] = ()
    b_drops_s: tuple[float, ...] = ()

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in ("genre_a", "genre_b", "bpm", "tempo_gap", "energy_a", "energy_b", "vocals",
                                               "a_stems", "b_stems", "b_acapella", "auto")}  # fmt: skip


SUNG_BAR = 0.5  # vocal_per_bar at or above this is a sung bar (musicreco.transitions.VOCAL_ON)
SUNG_SHARE = 0.25  # an edge with this share of sung bars sings
EDGE_BARS = 32  # the bars of A's end and B's start that count for the vocal state


def _sings(an: dict, end: bool) -> bool:
    v = np.asarray(an.get("vocal_per_bar") or [], float)
    v = v[-EDGE_BARS:] if end else v[:EDGE_BARS]
    return bool(v.size) and float(np.mean(v >= SUNG_BAR)) >= SUNG_SHARE


def _energy(an: dict) -> float | None:
    e = an.get("energy_per_bar") or []
    return round(float(np.mean(e)), 3) if len(e) else None


def _drops(an: dict) -> tuple[float, ...]:
    return tuple(float(s["start_s"]) for s in an.get("sections") or [] if s.get("kind") == "drop" and "start_s" in s)


def tempo_gap(a_bpm: float, b_bpm: float) -> float:
    """|B / A - 1|, with B at half or double time when that's closer."""
    if a_bpm <= 0 or b_bpm <= 0:
        return 0.0
    return round(min(abs(b_bpm * f / a_bpm - 1) for f in (0.5, 1.0, 2.0)), 4)


def context(a_an: dict, b_an: dict, *, genre_a: str | None = None, genre_b: str | None = None,
            energy_a: float | None = None, energy_b: float | None = None, a_stems: bool = False, b_stems: bool = False,
            b_acapella: bool = False) -> Context:  # fmt: skip
    """The pair's context from Seedbox 1-shaped analyses (`timeline.seedbox1_analysis`). An energy not given is the
    mean of the analysis's `energy_per_bar`."""
    sa, sb = _sings(a_an, True), _sings(b_an, False)
    return Context(
        genre_a=genre_a, genre_b=genre_b, bpm=float(a_an["bpm"]), tempo_gap=tempo_gap(float(a_an["bpm"]), float(b_an["bpm"])),
        energy_a=energy_a if energy_a is not None else _energy(a_an), energy_b=energy_b if energy_b is not None else _energy(b_an),
        vocals="both" if sa and sb else "a" if sa else "b" if sb else "none", a_stems=a_stems, b_stems=b_stems,
        b_acapella=b_acapella, a_duration_s=a_an.get("duration_s"), b_duration_s=b_an.get("duration_s"),
        a_drops_s=_drops(a_an), b_drops_s=_drops(b_an),
    )  # fmt: skip


def _inside(x: float | None, r: tuple[float, float] | None) -> bool:
    return r is None or (x is not None and r[0] <= x <= r[1])


def why_not(style: Style, ctx: Context) -> str | None:
    """None when the style applies to this pair before planning, else the first reason it doesn't."""
    w = style.when
    if w.genres:
        if ctx.genre_a not in w.genres:
            return "genre"
        if ctx.genre_b is not None and ctx.genre_b not in w.genres:
            return "genre of B"
    if w.bpm is not None and not (ctx.bpm is not None and (_inside(ctx.bpm, w.bpm) or _inside(2 * ctx.bpm, w.bpm))):
        return "bpm"
    if not _inside(ctx.energy_a, w.energy_a):
        return "energy of A"
    if not _inside(ctx.energy_b, w.energy_b):
        return "energy of B"
    if w.vocals and ctx.vocals not in w.vocals:
        return "vocals"
    gap = w.tempo_gap if w.tempo_gap is not None else (None if style.kind == "cut" or style.is_port else (0.0, MAX_TEMPO_GAP))
    if not _inside(ctx.tempo_gap, gap):
        return "tempo gap"
    if w.replaces and ctx.auto not in w.replaces:
        return "not instead of the generator's pick"
    for need in w.needs:
        if need in ("a_stems", "b_stems", "b_acapella") and not getattr(ctx, need):
            return need.replace("_", " ")
    return None


def score(style: Style, ctx: Context) -> float:
    s = style.weight + (AUTO_BONUS if style.technique == ctx.auto else 0.0)
    return s + max((style.boost.get(g, 0.0) for g in {ctx.genre_a, ctx.genre_b} if g), default=0.0)


def rank(ctx: Context, styles: tuple[Style, ...] | None = None) -> list[Style]:
    """The styles that apply, best first. A port of the generator's own pick always applies."""
    pool = all_styles() if styles is None else styles
    ok = [s for s in pool if why_not(s, ctx) is None]
    return sorted(ok, key=lambda s: -score(s, ctx))


# --- writing a style onto a plan -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    start: float
    end: float
    swap: float

    def at(self, where: str) -> float:
        m = _WHERE.match(where)
        assert m is not None
        anchor, sign, amount, unit = m.groups()
        base = {"start": self.start, "end": self.end, "swap": self.swap, "cut": 0.0}.get(anchor)
        if base is None:
            base = self.start + float(anchor) * (self.end - self.start)
        if amount is not None:
            beats = float(amount) * (4.0 if unit in ("bar", "bars") else 1.0)
            base += beats if sign == "+" else -beats
        return base


def _snap(beat: float) -> float:
    return round(beat * 16) / 16


def is_cut(plan: MixPlan) -> bool:
    """The base put B in on beat 0 at full, with A gone by then: there's no blend after the cut."""
    from musicreco.mixplan import CUTS

    return plan.technique in CUTS or float(np.asarray(plan.a.play_to_beat)) <= 4.0


def span_of(plan: MixPlan, style: Style) -> Span:
    if style.span is not None:
        s, e = style.span[0] * 4.0, style.span[1] * 4.0
    elif is_cut(plan):
        s, e = -4.0 * plan.bars, 0.0
    else:
        s, e = 0.0, 4.0 * plan.bars
    return Span(s, e, _snap(s + style.swap * (e - s)))


def _lane(points: tuple[Point, ...], span: Span) -> list[dict]:
    kfs = sorted(((_snap(span.at(p.where)), p.value, p.curve) for p in points), key=lambda k: k[0])
    return [{"beat": b, "value": v, "curve": c} for b, v, c in kfs]


def _flat_stems(start: float) -> dict:
    return {name: [{"beat": start, "value": 1.0, "curve": "linear"}] for name in STEM_LANES}


def _file_beats(deck: dict, duration_s: float | None) -> tuple[float, float]:
    """The master beats where the deck's file starts and ends (its grid, no loops)."""
    from ..plan import Deck

    grid = deck_grid(Deck.model_validate(deck))
    lo = float(grid.beat_at(0.0))
    hi = float(grid.beat_at(duration_s)) if duration_s else math.inf
    return lo, hi


def drops_on_cut(plan: MixPlan, ctx: Context) -> bool:
    """A drop of A's and one of B's both start on beat 0 (as a slam lines them up when A has a build to cut out of)."""
    for deck, drops in ((plan.a, ctx.a_drops_s), (plan.b, ctx.b_drops_s)):
        grid = deck_grid(deck)
        if not any(abs(float(grid.beat_at(t))) <= DROP_SNAP_BEATS for t in drops):
            return False
    return True


def apply(style: Style, base: MixPlan, ctx: Context) -> MixPlan | None:
    """The style written over the base generator's plan, or None when it doesn't fit this pair after all (a need the
    plan shows, a deck whose file can't cover the span)."""
    if "drops" in style.when.needs and not drops_on_cut(base, ctx):
        return None
    if style.is_port:
        return base.model_copy(update={"technique": style.technique})
    span = span_of(base, style)
    raw = base.model_dump()
    window = raw["window"]
    decks = {"a": raw["a"], "b": raw["b"]}
    for name, deck in decks.items():
        if not style.loops:
            deck["loops"] = []
        for lane, points in style.lanes.get(name, {}).items():
            deck["lanes"][lane] = _lane(points, span)
        if style.stems.get(name):
            deck["stem_lanes"] = deck["stem_lanes"] or _flat_stems(window["start_beat"])
            for lane, points in style.stems[name].items():
                deck["stem_lanes"][lane] = _lane(points, span)
        deck["sends"].update(style.sends.get(name, {}))
        if name in style.fx:
            deck["fx"] = [{"type": f.type, "start_beat": _snap(span.at(f.start)), "end_beat": _snap(span.at(f.end)),
                           **({"div": f.div} if f.div is not None else {})} for f in style.fx[name]]  # fmt: skip
            deck["loops"] = [lp for lp in deck["loops"]
                             if all(not (lp["start_beat"] < e["end_beat"] and e["start_beat"] < lp["end_beat"]) for e in deck["fx"])]  # fmt: skip
    a, b = decks["a"], decks["b"]
    _, a_hi = _file_beats(a, ctx.a_duration_s)
    last = max((_snap(span.at(p.where)) for points in style.lanes.get("a", {}).values() for p in points), default=-math.inf)
    if last > a["play_to_beat"] and a_hi > a["play_to_beat"]:
        # A's lanes go on past where it stops (an echo's feedback, a send's tail): its sends hear it play on, silent
        a["play_to_beat"] = min(last, a_hi)
    if style.span is not None:
        b_lo, _ = _file_beats(b, ctx.b_duration_s)
        sends = max((k["value"] for lane in ("echo_send", "reverb_send") for k in a["lanes"][lane]), default=0.0) > 0
        a_to = span.end + (SEND_TAIL if sends else 0.0)
        if a_to > a_hi or span.start < b_lo:
            return None
        a["play_to_beat"] = max(a["play_to_beat"], a_to) if not is_cut(base) else a_to
        b["play_from_beat"] = min(b["play_from_beat"], span.start)
    window["start_beat"] = min(window["start_beat"], span.start - LEAD_BEATS)
    window["end_beat"] = max(window["end_beat"], span.end + LEAD_BEATS)
    if window["end_beat"] - window["start_beat"] > 512:
        return None
    bars = base.bars if style.span is None else max(1, round((span.end - span.start) / 4.0))
    raw.update(technique=style.technique, bars=bars,
               notes=[*raw["notes"], f"Style: {style.label} (written over the generator's {style.base})."])  # fmt: skip
    try:
        return validate(raw)
    except ValueError:
        return None


Generate = Callable[[str | None, int | None], dict]  # (technique, bars) -> a Seedbox 1 plan for the pair


def plan(ctx: Context, generate: Generate, load: Callable[[dict], MixPlan],
         styles: tuple[Style, ...] | None = None) -> tuple[MixPlan, dict]:
    """The best style for the pair, written over its base generator's plan, and a report: the context, the style, and
    the styles tried. `generate(None, None)` is the generator's own pick, which is `ctx.auto` from here on."""
    auto_raw = generate(None, None)
    ctx = Context(**{**ctx.__dict__, "auto": auto_raw["technique"]})
    tried: list[dict] = []
    auto_plan: MixPlan | None = None
    for style in rank(ctx, styles):
        try:
            if style.base == ctx.auto and style.bars is None:
                auto_plan = auto_plan or load(auto_raw)
                base = auto_plan
            else:
                base = load(generate(style.base, style.bars))
        except ValueError as e:
            tried.append({"style": style.id, "why_not": f"the generator refused it: {e}"})
            continue
        out = apply(style, base, ctx)
        if out is not None:
            return out, {"style": style.id, "context": ctx.to_json(), "tried": tried}
        tried.append({"style": style.id, "why_not": "it doesn't fit the planned window"})
    fallback = load(auto_raw)  # the generator's pick: its port always applies, unless the files change under it
    return fallback, {"style": None, "context": ctx.to_json(), "tried": tried}


def snapshot(plan_: MixPlan) -> dict:
    """The parts of a plan a style decides, rounded, for snapshot tests."""

    def lanes(deck: Any, names: tuple[str, ...], defaults: dict[str, float]) -> dict:
        out = {}
        for name in names:
            kfs: list[Keyframe] = getattr(deck, name)
            if len(kfs) == 1 and abs(kfs[0].value - defaults[name]) < 1e-9:
                continue
            out[name] = [[round(k.beat, 4), round(k.value, 4), k.curve] for k in kfs]
        return out

    def deck(d: Any) -> dict:
        return {
            "play": [round(d.play_from_beat, 4), round(d.play_to_beat, 4)], "lanes": lanes(d.lanes, LANES, LANE_DEFAULTS),
            "stems": lanes(d.stem_lanes, STEM_LANES, dict.fromkeys(STEM_LANES, 1.0)) if d.stem_lanes else None,
            "sends": {"echo_beats": d.sends.echo_beats, "reverb_size": d.sends.reverb_size},
            "fx": [e.model_dump() for e in d.fx], "loops": [lp.model_dump() for lp in d.loops],
        }  # fmt: skip

    return {"technique": plan_.technique, "bars": plan_.bars, "window": [plan_.window.start_beat, plan_.window.end_beat],
            "a": deck(plan_.a), "b": deck(plan_.b)}  # fmt: skip
