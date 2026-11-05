"""`python -m seedbox2.eval energy`: calibrate the track energy (#48) against hand ratings.

    python -m seedbox2.eval energy template > ratings.toml       # the golden corpus's tracks, each with rating = 0
    python -m seedbox2.eval energy fit ratings.toml [--audio DIR]... [--cache FILE]
    python -m seedbox2.eval energy scale FILE_OR_DIR... [--cache FILE]

`ratings.toml` is a list of `[[track]]`, each `rating = 1..10` with either `file = "path"` or `id = "<golden.toml id>"`
(found by hash, as the golden corpus does). A rating of 0 means not rated, and is left out.

`fit` measures the rated tracks and fits non-negative weights and the linear scale, then prints the leave-one-out
Spearman correlation and mean error, which are what to judge it by, and the `CALIBRATION` to paste into
`seedbox2/analysis/energy.py`. `scale` keeps the weights and sets the scale from the distribution of real tracks: their
5th percentile is a 3 and their 95th a 9. Bump `energy.VERSION` after changing the calibration.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path

import numpy as np
from scipy import optimize, stats

from seedbox2.analysis import energy

AUDIO = (".mp3", ".flac", ".m4a", ".aiff", ".aif", ".wav", ".ogg", ".opus")


def track_features(path: Path, cache: dict[str, dict]) -> dict[str, float]:
    """The file's whole-track features, without a beat grid: at this level the grid hardly matters."""
    key = str(path)
    if key not in cache:
        frames, curve = energy.measure(path)
        data = energy.analyse(frames, curve, None)
        cache[key] = {**data["track_features"], "integrated_lufs": curve["integrated_lufs"]}
    return {k: cache[key][k] for k in energy.FEATURES}


def matrix(rows: Iterable[dict[str, float]]) -> np.ndarray:
    return np.array([[r[k] for k in energy.FEATURES] for r in rows], np.float64)


def fit(x: np.ndarray, y: np.ndarray) -> dict:
    """rating ≈ a x (w · features) + b, with w ≥ 0 and summing to 1, by non-negative least squares."""
    design = np.hstack([x, np.ones((x.shape[0], 1)), -np.ones((x.shape[0], 1))])
    c, _ = optimize.nnls(design, y)
    a = float(c[: x.shape[1]].sum())
    if a <= 0:
        raise ValueError("the ratings don't rise with any feature")
    weights = {k: round(float(v / a), 4) for k, v in zip(energy.FEATURES, c, strict=False)}
    return {"weights": weights, "scale": [round(a, 3), round(float(c[-2] - c[-1]), 3)], "ratings": int(y.size)}


def predict(x: np.ndarray, calibration: dict) -> np.ndarray:
    w = np.array([calibration["weights"][k] for k in energy.FEATURES])
    a, b = calibration["scale"]
    return np.clip(a * (x @ w) + b, 1.0, 10.0)


def leave_one_out(x: np.ndarray, y: np.ndarray) -> dict:
    predicted = np.zeros_like(y, dtype=np.float64)
    for i in range(y.size):
        keep = np.arange(y.size) != i
        predicted[i] = predict(x[i : i + 1], fit(x[keep], y[keep]))[0]
    return {"spearman": round(float(stats.spearmanr(predicted, y).statistic), 3),
            "mae": round(float(np.abs(predicted - y).mean()), 3),
            "within_1": round(float((np.abs(predicted - y) <= 1).mean()), 3)}  # fmt: skip


def scale_from(x: np.ndarray, weights: dict[str, float]) -> list[float]:
    s = x @ np.array([weights[k] for k in energy.FEATURES])
    lo, hi = np.percentile(s, [5, 95])
    a = 6.0 / max(hi - lo, 1e-6)
    return [round(float(a), 3), round(float(3.0 - a * lo), 3)]


def _files(items: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for item in items:
        p = Path(item).expanduser()
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.suffix.lower() in AUDIO)
        else:
            out.append(p)
    return out


def _load_cache(path: Path | None) -> dict:
    return json.loads(path.read_text()) if path and path.is_file() else {}


def _measure(paths: list[Path], cache_file: Path | None) -> tuple[list[Path], list[dict]]:
    cache, kept, rows = _load_cache(cache_file), [], []
    for i, p in enumerate(paths):
        try:
            rows.append(track_features(p, cache))
            kept.append(p)
        except Exception as e:  # noqa: BLE001 - one unreadable file doesn't end a calibration
            print(f"skipping {p}: {e}", file=sys.stderr)
        print(f"{i + 1}/{len(paths)} {p.name}", file=sys.stderr)
        if cache_file:
            cache_file.write_text(json.dumps(cache))
    return kept, rows


def _rated(ratings: Path, audio: list[Path]) -> list[tuple[Path, float]]:
    from seedbox2.eval import golden

    entries = [t for t in tomllib.loads(ratings.read_text(encoding="utf-8")).get("track", []) if t.get("rating")]
    by_id = {}
    if any("id" in t for t in entries):
        corpus = golden.load_corpus()
        wanted = {t["id"] for t in entries if "id" in t}
        tracks = [t for t in corpus.tracks.values() if t.id in wanted]
        by_id = golden.find_audio(tracks, golden.audio_folders(audio))
    out = []
    for t in entries:
        if not 1 <= t["rating"] <= 10:
            raise SystemExit(f"a rating is 1 to 10: {t}")
        path = Path(t["file"]).expanduser() if "file" in t else by_id.get(t["id"])
        if path is None:
            print(f"no audio for {t['id']}", file=sys.stderr)
        else:
            out.append((Path(path), float(t["rating"])))
    return out


def cmd_template() -> int:
    from seedbox2.eval import golden

    for t in golden.load_corpus().tracks.values():
        print(f'[[track]]\n# {t.artist} - {t.title} ({t.genre})\nid = "{t.id}"\nrating = 0\n')
    return 0


def cmd_fit(a: argparse.Namespace) -> int:
    rated = _rated(a.ratings, a.audio)
    if len(rated) < 3 * len(energy.FEATURES):
        raise SystemExit(f"{len(rated)} rated tracks with audio: fitting {len(energy.FEATURES)} weights takes at least "
                         f"{3 * len(energy.FEATURES)}")  # fmt: skip
    ratings = dict(rated)
    paths, rows = _measure(list(ratings), a.cache)
    x, y = matrix(rows), np.array([ratings[p] for p in paths])
    before = predict(x, energy.CALIBRATION)
    print(json.dumps({"tracks": int(y.size),
                      "current": {"spearman": round(float(stats.spearmanr(before, y).statistic), 3),
                                  "mae": round(float(np.abs(before - y).mean()), 3)},
                      "leave_one_out": leave_one_out(x, y), "CALIBRATION": fit(x, y)}, indent=1))  # fmt: skip
    return 0


def cmd_scale(a: argparse.Namespace) -> int:
    paths, rows = _measure(_files(a.files), a.cache)
    if not paths:
        raise SystemExit("no audio")
    x = matrix(rows)
    weights = energy.CALIBRATION["weights"]
    scale = scale_from(x, weights)
    print(json.dumps({"tracks": len(paths), "feature_means": dict(zip(energy.FEATURES, np.round(x.mean(0), 3).tolist(), strict=True)),
                      "CALIBRATION": {"weights": weights, "scale": scale, "ratings": 0}}, indent=1))  # fmt: skip
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m seedbox2.eval energy", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("template", help="print a ratings file for the golden corpus")
    f = sub.add_parser("fit", help="fit the calibration to hand ratings")
    f.add_argument("ratings", type=Path)
    f.add_argument("--audio", action="append", default=[], help="a folder with the golden audio (also $SEEDBOX2_GOLDEN_AUDIO)")
    f.add_argument("--cache", type=Path, help="remember measured features in this JSON file")
    s = sub.add_parser("scale", help="set the 1-10 scale from real tracks, keeping the weights")
    s.add_argument("files", nargs="+")
    s.add_argument("--cache", type=Path, help="remember measured features in this JSON file")
    a = p.parse_args(argv)
    if a.cmd == "template":
        return cmd_template()
    return cmd_fit(a) if a.cmd == "fit" else cmd_scale(a)


if __name__ == "__main__":
    sys.exit(main())
