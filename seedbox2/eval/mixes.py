"""`python -m seedbox2.eval mixes`: render a set of track pairs with every engine and compare the metrics (#54).

    python -m seedbox2.eval mixes                     # built-in synthetic pairs (no music needed)
    python -m seedbox2.eval mixes pairs.toml          # your own pairs
    python -m seedbox2.eval mixes pairs.toml --engines seedbox1 --json out.json --keep renders/

pairs.toml (paths relative to the file; audio stays local; the golden corpus, #87, is in golden.py):

    [[pair]]
    name = "house into house"      # optional
    a = "music/a.mp3"
    b = "music/b.mp3"
    key_a = "8A"                   # optional: Camelot keys (default: the files' TKEY tags)
    key_b = "9A"
    technique = "bass swap"        # optional: force a Seedbox 1 technique / blend length
    bars = 16

Engines: `seedbox1` plans with musicreco.mixplan and renders with musicreco.mixrender. `seedbox2` starts from the same
plan with the automation fitted to the audio (#56, seedbox2/mix/automation.py) and plays the tracks' stems when they
have them; its own planning and stretching land with #52 and #53.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .mixmetrics import Render, measure

SR = 44100


@dataclass
class Pair:
    name: str
    a: Path
    b: Path
    key_a: str = ""
    key_b: str = ""
    technique: str | None = None
    bars: int | None = None
    hash_a: str = ""  # the library's hashes: how a render finds the tracks' cached stems
    hash_b: str = ""


class EngineUnavailable(Exception):
    """The engine can't render yet (or is missing a dependency): its results read n/a."""


# --- engines ---------------------------------------------------------------------------------------------------------

_analyses: dict[Path, dict] = {}


def _analysis(path: Path) -> dict:
    from musicreco import structure

    if path not in _analyses:
        _analyses[path] = structure.analyze(path, track_id=path.stem)
    return _analyses[path]


def analysis_of(pair: Pair) -> tuple[dict, dict]:
    return _analysis(pair.a), _analysis(pair.b)


def _seedbox1_plan(pair: Pair) -> tuple[dict, dict[str, str]]:
    from musicreco import mixplan
    from musicreco.mixpair import _track

    an_a, an_b = _analysis(pair.a), _analysis(pair.b)
    a, b = _track(pair.a, an_a, pair.key_a or None), _track(pair.b, an_b, pair.key_b or None)
    plan = mixplan.generate(a, an_a, b, an_b, str(pair.a.parent), technique=pair.technique, bars=pair.bars)
    return plan, {"A": a.camelot, "B": b.camelot}


def render_seedbox1(pair: Pair) -> Render:
    from musicreco import mixrender

    plan, keys = _seedbox1_plan(pair)
    return Render.from_mixrender(mixrender.render_audio(plan, pair.a, pair.b, SR), plan, keys)


def render_seedbox2(pair: Pair) -> Render:
    from ..mix import offline

    raw, keys = _seedbox1_plan(pair)
    r, plan = offline.render_adapted(raw, pair.a, pair.b, offline.cached_stems(pair.hash_a),
                                     offline.cached_stems(pair.hash_b), SR)  # fmt: skip
    return Render.from_mixrender(r, plan, keys)


ENGINES: dict[str, Callable[[Pair], Render]] = {"seedbox1": render_seedbox1, "seedbox2": render_seedbox2}


# --- synthetic pairs -------------------------------------------------------------------------------------------------

_PC = {f"{n}{l}": pc for l, base in (("B", 11), ("A", 8)) for n, pc in
       ((n, (base + 7 * (n - 1)) % 12) for n in range(1, 13))}  # Camelot -> root pitch class (1B = B, 1A = Ab)  # fmt: skip


def synth_track(bpm: float, bars: int, camelot: str, intro: int, outro: int, vocals: list[tuple[int, int]],
                sr: int = SR, seed: int = 0) -> np.ndarray:
    """A plain four-on-the-floor track (2, n): kick every beat, off-beat hats, a bassline in the body (not in the
    drum-only intro/outro), a sung-like line (harmonic tone with vibrato, sung 3 bars of every 4) in `vocals` bars."""
    rng = np.random.default_rng(seed)
    spb = 60.0 / bpm
    n = int((bars * 4 * spb + 1.0) * sr)
    x = np.zeros(n)
    root = 36 + _PC.get(camelot.upper(), 9)  # C2 + pitch class
    f_bass = 440 * 2 ** ((root - 69) / 12)
    kl = int(0.25 * sr)
    tk = np.arange(kl) / sr
    kick = np.sin(2 * np.pi * np.cumsum(50 + 120 * np.exp(-tk * 30)) / sr) * np.exp(-tk * 9)
    hl = int(0.04 * sr)
    hat = rng.standard_normal(hl) * np.exp(-np.arange(hl) / sr * 90)
    hat = np.diff(np.diff(hat, prepend=0), prepend=0) * 0.15
    bl = int(spb * 0.45 * sr)
    tb = np.arange(bl) / sr
    bass = np.sin(2 * np.pi * f_bass * tb) * np.minimum(1, tb * 200) * np.exp(-tb * 4) * 0.5
    for beat in range(bars * 4):
        bar = beat // 4
        i = int(beat * spb * sr)
        x[i: i + kl] += 0.8 * kick[: n - i]
        j = int((beat + 0.5) * spb * sr)
        x[j: j + hl] += hat[: n - j]
        if intro <= bar < bars - outro:
            x[j: j + bl] += bass[: n - j]
    t = np.arange(n) / sr
    f_voc = f_bass * 4 * (1 + 0.012 * np.sin(2 * np.pi * 5.5 * t))
    ph = 2 * np.pi * np.cumsum(f_voc) / sr
    voice = sum(np.sin(h * ph) / h for h in range(1, 7)) * 0.12
    sung = np.zeros(n)
    for v0, v1 in vocals:
        for bar in range(v0, v1):
            if bar % 4 != 3:
                i0, i1 = int(bar * 4 * spb * sr), int((bar + 1) * 4 * spb * sr)
                sung[i0:i1] = 1
    sung = np.convolve(sung, np.ones(int(0.02 * sr)) / int(0.02 * sr), mode="same")
    x += voice * sung
    x *= 0.5 / max(1e-9, np.abs(x).max())
    return np.stack([x, x]).astype(np.float32)


SYNTHETIC = [  # name, (bpm, camelot, intro, outro, vocals) for A and B
    ("same tempo, same key", (124.0, "8A", 16, 16, []), (124.0, "8A", 16, 16, [])),
    ("small tempo gap, +1", (124.0, "8A", 16, 16, []), (126.5, "9A", 16, 16, [])),
    ("vocal outro into vocal intro", (124.0, "8A", 16, 16, [(24, 48)]), (124.0, "8A", 16, 16, [(0, 24)])),
    ("key clash", (124.0, "8A", 16, 16, []), (124.0, "3B", 16, 16, [])),
    ("big tempo gap", (124.0, "8A", 16, 16, []), (140.0, "8A", 16, 16, [])),
]


def synthetic_pairs(folder: Path) -> list[Pair]:
    """Write the SYNTHETIC tracks (48 bars each) as WAVs into folder; the pairs that use them."""
    from scipy.io import wavfile

    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i, (name, sa, sb) in enumerate(SYNTHETIC):
        paths = []
        for side, (bpm, key, intro, outro, voc) in (("a", sa), ("b", sb)):
            p = folder / f"pair{i}_{side}.wav"
            if not p.exists():
                wavfile.write(p, SR, synth_track(bpm, 48, key, intro, outro, voc, seed=2 * i + (side == "b")).T)
            paths.append(p)
        out.append(Pair(name, paths[0], paths[1], sa[1], sb[1]))
    return out


def load_pairs(path: Path) -> list[Pair]:
    data = tomllib.loads(path.read_text())
    out = []
    for i, p in enumerate(data.get("pair", [])):
        a, b = (path.parent / p[k] for k in ("a", "b"))
        out.append(Pair(p.get("name") or f"{a.stem} → {b.stem}", a, b, p.get("key_a", ""), p.get("key_b", ""),
                        p.get("technique"), p.get("bars")))
    if not out:
        raise ValueError(f"{path}: no [[pair]] entries")
    return out


# --- table -----------------------------------------------------------------------------------------------------------

COLUMNS = [  # header, width, getter
    ("overlap s", 9, lambda m: m["overlap"]["seconds"]),
    ("beat ms", 7, lambda m: m["beat"]["median_ms"]),
    ("bass s", 6, lambda m: m["bass"]["seconds"]),
    ("bass dB·s", 9, lambda m: m["bass"]["db_s"]),
    ("vocal s", 7, lambda m: m["vocals"]["seconds"]),
    ("dip LU", 6, lambda m: m["loudness"]["dip_lu"]),
    ("bump LU", 7, lambda m: m["loudness"]["bump_lu"]),
    ("TP dBTP", 7, lambda m: m["loudness"]["true_peak_dbtp"]),
    ("clips", 5, lambda m: m["loudness"]["clipped_samples"]),
    ("mask lo", 7, lambda m: m["masking"]["bass"]),
    ("mask mid", 8, lambda m: m["masking"]["low_mid"]),
    ("key", 3, lambda m: m["key"]["key_steps"]),
    ("stretch%", 8, lambda m: m["key"]["max_stretch_pct"]),
    ("checks", 6, lambda m: "".join({"pass": "✓", "warn": "!", "fail": "✗", "na": "·"}[c["status"]]
                                    for c in m["checks"])),
]


def _cell(v: object, width: int) -> str:
    if v is None:
        s = "–"
    elif isinstance(v, float):
        s = f"{v:.2f}" if abs(v) < 10 else f"{v:.1f}"
    else:
        s = str(v)
    return s.rjust(width)


def table(results: list[dict], engines: list[str]) -> str:
    name_w = max([len("pair / engine")] + [len(r["pair"]) for r in results] + [len(e) + 2 for e in engines])
    head = "pair / engine".ljust(name_w) + "".join("  " + h.rjust(w) for h, w, _ in COLUMNS)
    lines = [head, "-" * len(head)]
    for r in results:
        lines.append(r["pair"])
        for e in engines:
            res = r["engines"][e]
            row = ("  " + e).ljust(name_w)
            if "error" in res:
                lines.append(row + "  n/a: " + res["error"])
                continue
            lines.append(row + "".join("  " + _cell(get(res["metrics"]), w) for _, w, get in COLUMNS))
    lines.append("-" * len(head))
    for e in engines:  # mean over the pairs the engine rendered
        ok = [r["engines"][e]["metrics"] for r in results if "metrics" in r["engines"][e]]
        row = f"mean {e}".ljust(name_w)
        if not ok:
            lines.append(row + "  n/a")
            continue
        cells = []
        for h, w, get in COLUMNS:
            vals = [get(m) for m in ok]
            if h == "checks":
                fails = sum(v.count("✗") for v in vals)
                cells.append(f"{fails}✗".rjust(w))
                continue
            nums = [float(v) for v in vals if isinstance(v, (int, float)) and math.isfinite(v)]
            cells.append(_cell(sum(nums) / len(nums) if nums else None, w))
        lines.append(row + "".join("  " + c for c in cells))
    lines.append("checks: beat sync, bass, vocals, loudness, key/tempo, low-end masking (✓ pass, ! warn, ✗ fail)")
    return "\n".join(lines)


# --- main ------------------------------------------------------------------------------------------------------------


def run(pairs: list[Pair], engines: list[str], keep: Path | None = None,
        log: Callable[[str], None] = lambda s: None) -> list[dict]:
    results = []
    for i, pair in enumerate(pairs):
        row = {"pair": pair.name, "a": str(pair.a), "b": str(pair.b), "engines": {}}
        for e in engines:
            log(f"[{i + 1}/{len(pairs)}] {pair.name}: {e} ...")
            try:
                t0 = time.perf_counter()
                r = ENGINES[e](pair)
                t1 = time.perf_counter()
                m = measure(r)
                t2 = time.perf_counter()
            except EngineUnavailable as ex:
                row["engines"][e] = {"error": str(ex)}
                continue
            except (ValueError, OSError) as ex:
                row["engines"][e] = {"error": f"{type(ex).__name__}: {ex}"}
                continue
            row["engines"][e] = {"metrics": m, "render_s": round(t1 - t0, 2), "metrics_s": round(t2 - t1, 2)}
            if keep is not None:
                from scipy.io import wavfile

                keep.mkdir(parents=True, exist_ok=True)
                wavfile.write(keep / f"pair{i}_{e}.wav", r.sr, np.ascontiguousarray(r.mix.T))
        results.append(row)
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m seedbox2.eval mixes", description=__doc__.split("\n\n")[0])
    p.add_argument("pairs", nargs="?", type=Path, help="pairs.toml (default: the built-in synthetic pairs)")
    p.add_argument("--engines", default=",".join(ENGINES), help=f"comma-separated (default: {','.join(ENGINES)})")
    p.add_argument("--json", type=Path, help="also write every metric and check as JSON here")
    p.add_argument("--keep", type=Path, help="write each render as DIR/pair<i>_<engine>.wav")
    p.add_argument("--work", type=Path, help="where the synthetic tracks go (default: a temporary folder)")
    args = p.parse_args(argv)
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    bad = [e for e in engines if e not in ENGINES]
    if bad:
        p.error(f"unknown engine(s) {', '.join(bad)}; choose from {', '.join(ENGINES)}")
    import tempfile

    with tempfile.TemporaryDirectory(prefix="seedbox2-eval-") as tmp:
        try:
            pairs = load_pairs(args.pairs) if args.pairs else synthetic_pairs(args.work or Path(tmp))
        except (ValueError, OSError, tomllib.TOMLDecodeError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        results = run(pairs, engines, args.keep, log=lambda s: print(s, file=sys.stderr))
    print(table(results, engines))
    if args.json:
        args.json.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    return 0
