"""Learnt cue points and transition lengths (#55): a small logistic model over the planner's own candidates.

`transitions.blend_windows` enumerates every phrase-aligned overlap of A's ending with B's beginning. Seedbox 1
scores each one with hand-set per-bar penalties (`transitions._window`). With a trained model installed, the score
is learnt from real DJ mixes instead: `features()` describes a candidate, a conditional logit over the candidates of
one transition picks the DJ's, and `CueModel.fit()` maps its utility onto the planner's 0-100 fit.

The hard constraints don't move: candidates are only ever phrase-aligned (the enumeration), and `transitions._plan`
still rejects a blend with too many bars of vocal over vocal or of clashing keys over each other.

Without a model nothing changes: `active()` is None and `_window` keeps its penalties. A model is plain JSON (a few
KB): see `seedbox2/eval/cues.py` for training and evaluation, and seedbox2/README.md for the data.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .transitions import Grid, Loop

FORMAT = 1
MODEL_ENV = "SEEDBOX_CUE_MODEL"  # path to a model; else <SEEDBOX_SUPPORT_DIR>/cue_model.json when it exists
SUPPORT_DIR_ENV = "SEEDBOX_SUPPORT_DIR"
MODEL_FILE = "cue_model.json"

LENGTHS = (8, 16, 32, 64)
ENERGY_BUCKETS = ("low", "mid", "high")
# Prior over transition lengths before any mix is seen. Kim et al. (ISMIR 2020) find DJs' transitions cluster at
# multiples of 32 beats (8 bars); the weights are ours, set by hand, until `seedbox2.eval cues train` measures them.
DEFAULT_LENGTH_PRIOR = {8: 0.2, 16: 0.35, 32: 0.35, 64: 0.1}

FEATURES = (
    "a_level32",  # A's in-point opens a 32-bar phrase
    "a_level16",  # ... at least a 16-bar one
    "b_level32",  # B's cue opens a 32-bar phrase
    "b_level16",
    "b_first_cue",  # B cued on its first phrase start
    "a_in_outro",  # A's in-point is in its outro
    "a_body_cut",  # phrases of A's body cut off before its outro (A out before its outro starts)
    "a_outro_skip",  # phrases of A's outro never played
    "a_body_under",  # phrases of A's body under the overlap
    "b_intro_skip",  # phrases of B's intro skipped by cueing later
    "b_body_under",  # share of the overlap with B's body already running
    "b_solo",  # phrases of B's intro still running after A is out
    "a_perc",  # A is drums-only at its in-point
    "b_perc",  # B is drums-only at its cue
    "a_drop_in",  # a drop of A's inside the overlap
    "b_drop_in",  # a drop of B's inside the overlap
    "low_a",  # A's mean low end (kick/bass stem activity) over the overlap
    "low_b",
    "low_both",  # both decks' low ends at once
    "energy_delta",  # B's energy at the end of the overlap minus A's at its start
    "energy_abs",
    "vocal_a",  # mean vocal activity over the overlap
    "vocal_b",
    "vocal_soft",  # share of the overlap with (soft) vocal over vocal
    "tonal_clash",  # share of the overlap with music on both decks while the keys clash
    "loops",  # phrases looped to stretch the blend
    "len8",  # length, against 16 bars
    "len32",
    "len_prior",  # log of the length prior for the pair's energy
)


def energy_bucket(energy: float) -> str:
    """Bucket of a track's mean per-bar energy (0..1)."""
    return "low" if energy < 0.45 else "mid" if energy < 0.7 else "high"


def snap_length(bars: float) -> int:
    """Nearest of 8/16/32/64 bars, on a log scale."""
    return min(LENGTHS, key=lambda n: abs(math.log2(max(bars, 1.0)) - math.log2(n)))


@dataclass
class CueModel:
    weights: dict[str, float]
    scale: float = 1.0  # fit = 100 * sigmoid(scale * utility + bias)
    bias: float = 0.0
    # length prior: "genre/energy" -> {bars: p}; "*/energy" and "*/*" are the fallbacks
    length_prior: dict[str, dict[int, float]] = field(default_factory=lambda: {"*/*": dict(DEFAULT_LENGTH_PRIOR)})
    info: dict[str, Any] = field(default_factory=dict)

    def utility(self, x: dict[str, float]) -> float:
        return sum(self.weights.get(k, 0.0) * v for k, v in x.items())

    def fit(self, x: dict[str, float]) -> float:
        z = self.scale * self.utility(x) + self.bias
        return 100.0 / (1.0 + math.exp(-max(-40.0, min(40.0, z))))

    def prior(self, length: int, energy: float, genre: str = "") -> float:
        b = energy_bucket(energy)
        for key in (f"{genre.lower()}/{b}", f"*/{b}", "*/*"):
            if key in self.length_prior:
                return self.length_prior[key].get(length, 1e-3)
        return DEFAULT_LENGTH_PRIOR.get(length, 1e-3)

    def to_json(self) -> dict:
        return {
            "format": FORMAT, "features": list(FEATURES),
            "weights": {k: round(self.weights.get(k, 0.0), 5) for k in FEATURES},
            "scale": round(self.scale, 5), "bias": round(self.bias, 5),
            "length_prior": {k: {str(n): round(p, 4) for n, p in v.items()} for k, v in self.length_prior.items()},
            "info": self.info,
        }  # fmt: skip

    @classmethod
    def from_json(cls, d: dict) -> CueModel:
        if d.get("format") != FORMAT:
            raise ValueError(f"cue model format {d.get('format')!r}, expected {FORMAT}")
        return cls(
            weights={k: float(v) for k, v in d["weights"].items() if k in FEATURES},
            scale=float(d.get("scale", 1.0)), bias=float(d.get("bias", 0.0)),
            length_prior={k: {int(n): float(p) for n, p in v.items()} for k, v in (d.get("length_prior") or {}).items()}
            or {"*/*": dict(DEFAULT_LENGTH_PRIOR)},
            info=dict(d.get("info") or {}),
        )  # fmt: skip

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=1) + "\n")

    @classmethod
    def load(cls, path: Path) -> CueModel:
        return cls.from_json(json.loads(path.read_text()))


def features(
    a: Grid, b: Grid, a0: int, b0: int, length: int, key_clash: bool, a_loop: Loop | None, b_loop: Loop | None,
    prior: CueModel | None = None,
) -> dict[str, float]:
    """One candidate overlap (A bars from a0 under B bars from b0, `length` bars, through the loops) as features."""
    from .transitions import _VOCAL_FLOOR, PHRASE, VOCAL_ON, deck_bar, first_cue

    xa, xb = (a_loop.bars if a_loop else 0), (b_loop.bars if b_loop else 0)
    e = a0 + length - xa
    o0, end = a.outro
    i0, i1 = b.intro
    ab = [min(a.n - 1, deck_bar(a0, a_loop, k)) for k in range(length)]
    bb = [min(b.n - 1, deck_bar(b0, b_loop, k)) for k in range(length)]
    L = float(length)
    vocal_soft = sum(
        max(0.0, min(a.vocal[ab[k]], b.vocal[bb[k]]) - _VOCAL_FLOOR) / (1 - _VOCAL_FLOOR) for k in range(length)
    )
    tonal = sum(not (a.perc[ab[k]] or b.perc[bb[k]]) for k in range(length)) if key_clash else 0
    low_a = sum(a.low[k] for k in ab) / L
    low_b = sum(b.low[k] for k in bb) / L
    ea = a.energy[ab[0]] if a.energy else 0.0
    eb = b.energy[bb[-1]] if b.energy else 0.0
    mean_e = (a.mean(a.energy, a0, a0 + length) + b.mean(b.energy, b0, b0 + length)) / 2
    model = prior or active()
    p = model.prior(length, mean_e) if model else DEFAULT_LENGTH_PRIOR.get(length, 1e-3)
    return {
        "a_level32": float(a.level(a0) >= 32),
        "a_level16": float(a.level(a0) >= 16),
        "b_level32": float(b.level(b0) >= 32),
        "b_level16": float(b.level(b0) >= 16),
        "b_first_cue": float(b0 == first_cue(b)),
        "a_in_outro": float(a0 >= o0),
        "a_body_cut": max(0, o0 - e) / PHRASE,
        "a_outro_skip": max(0, end - max(e, o0)) / PHRASE,
        "a_body_under": max(0, min(o0, e) - a0) / PHRASE,
        "b_intro_skip": max(0, b0 - i0) / PHRASE,
        "b_body_under": max(0, length - max(i1 - b0, 0) - xb) / L,
        "b_solo": max(0, i1 - (b0 + length - xb)) / PHRASE,
        "a_perc": float(a.perc[min(a0, a.n - 1)]),
        "b_perc": float(b.perc[min(b0, b.n - 1)]),
        "a_drop_in": float(any(a0 <= d < a0 + length for d in a.drops)),
        "b_drop_in": float(any(b0 <= d < b0 + length for d in b.drops)),
        "low_a": low_a,
        "low_b": low_b,
        "low_both": low_a * low_b,
        "energy_delta": eb - ea,
        "energy_abs": abs(eb - ea),
        "vocal_a": sum(a.vocal[k] >= VOCAL_ON for k in ab) / L,
        "vocal_b": sum(b.vocal[k] >= VOCAL_ON for k in bb) / L,
        "vocal_soft": vocal_soft / L,
        "tonal_clash": tonal / L,
        "loops": (xa + xb) / PHRASE,
        "len8": float(length == 8),
        "len32": float(length == 32),
        "len_prior": math.log(max(p, 1e-3)),
    }


_cache: dict[str, tuple[float, CueModel | None]] = {}
_override: list[CueModel | None] = []


def model_path(environ: Any = os.environ) -> Path | None:
    raw = environ.get(MODEL_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    support = environ.get(SUPPORT_DIR_ENV, "").strip()
    if support and (p := Path(support).expanduser() / MODEL_FILE).is_file():
        return p
    return None


def active() -> CueModel | None:
    """The installed model (re-read when its file changes), or None: the planner keeps its hand-set penalties."""
    if _override:
        return _override[-1]
    path = model_path()
    if path is None or not path.is_file():
        return None
    mtime = path.stat().st_mtime
    hit = _cache.get(str(path))
    if hit is None or hit[0] != mtime:
        try:
            hit = (mtime, CueModel.load(path))
        except (OSError, ValueError, KeyError, TypeError) as exc:  # a broken file falls back to the penalties
            import logging

            logging.getLogger(__name__).warning("ignoring cue model %s: %s", path, exc)
            hit = (mtime, None)
        _cache[str(path)] = hit
    return hit[1]


class using:
    """`with cuescore.using(model):` scores with `model` (None: the hand-set penalties) whatever is installed."""

    def __init__(self, model: CueModel | None) -> None:
        self.model = model

    def __enter__(self) -> CueModel | None:
        _override.append(self.model)
        return self.model

    def __exit__(self, *exc: object) -> None:
        _override.pop()
