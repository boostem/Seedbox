"""Measures a whole set's export on synthetic tracks: `python -m tests.measure_set_export --hours 2 [--stems]`.

It makes MP3s, then renders in a fresh process and prints its peak RSS (the ticket's limit is 1 GB, #70), how long each
stage took and what the mix measures. Not a pytest test: a two-hour set takes minutes and a few GB of scratch disk.
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from musicreco import mixplan
from musicreco.paths import ffmpeg_path
from seedbox2.mix import convert, setchain, setexport
from seedbox2.mix.plan import MixPlan
from seedbox2.mix.setrender import Source

from .fixtures import Spec, analysis, click_track, pad_track, track

SR = 44100
TRACK_BARS = 160
STEMS = ("drums", "bass", "vocals", "other")


def _specs(hours: float) -> list[Spec]:
    specs, seconds = [], 0.0
    while seconds < hours * 3600:
        bpm = 124.0 + 3.0 * np.sin(len(specs))
        specs.append(Spec(f"t{len(specs)}", bpm=round(float(bpm), 2), bars=TRACK_BARS, intro_bars=16, outro_bars=16))
        seconds += specs[-1].duration_s * 0.85
    return specs


def _make_file(spec: Spec, seed: int, folder: Path) -> Path:
    out = folder / f"{spec.id}.mp3"
    if not out.exists():
        wav = folder / f"{spec.id}.wav"
        x, y = click_track(spec, SR, 800.0 + 40 * seed), pad_track(spec, SR, 110.0 + 7 * seed)
        n = min(x.shape[1], y.shape[1])
        wavfile.write(wav, SR, (0.4 * x[:, :n] + 0.4 * y[:, :n]).astype(np.float32).T)
        subprocess.run([ffmpeg_path(), "-nostdin", "-loglevel", "error", "-y", "-i", str(wav), "-b:a", "192k", str(out)], check=True)
        wav.unlink()
    return out


def _with_stems(plan: MixPlan) -> MixPlan:
    """Every stem at 1, with the bass moving through the blend: the plan that plays the most stems."""
    start, end = setchain.blend_span(plan)
    raw = plan.model_dump()
    still = [{"beat": plan.window.start_beat, "value": 1.0, "curve": "linear"}]
    moving = [*still, {"beat": start, "value": 1.0, "curve": "linear"}, {"beat": end, "value": 0.5, "curve": "linear"},
              {"beat": end + 4, "value": 1.0, "curve": "linear"}]  # fmt: skip
    for side in ("a", "b"):
        raw[side]["stem_lanes"] = {"gain_drums": still, "gain_bass": moving, "gain_vocals": still, "gain_other": still}
    return MixPlan.model_validate(raw)


def _rss_mb() -> int:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)


def child(args: argparse.Namespace) -> None:
    specs = _specs(args.hours)
    folder = Path(args.folder)
    files = [folder / f"{s.id}.mp3" for s in specs]
    sources = [Source(s.id, s.id, f, -2.0, {n: f for n in STEMS} if args.stems else None) for s, f in zip(specs, files)]
    plans = []
    for a, b in zip(specs, specs[1:]):
        plan = convert.load(mixplan.generate(track(a), analysis(a), track(b), analysis(b), ""), from_track=a.id, to_track=b.id)
        plans.append(_with_stems(plan) if args.stems else plan)
    chain = setchain.plan_chain(plans, specs[-1].duration_s)
    spool = setexport.Spool(folder / "spool")
    times = {}
    t = time.monotonic()
    total, count = setexport.render_bus(chain, sources, spool, sr=SR)
    times["render_s"] = round(time.monotonic() - t, 1)
    times["render_rss_mb"] = _rss_mb()
    t = time.monotonic()
    makeup = setexport.find_makeup(spool, count, -14.0, SR)
    times["makeup_s"] = round(time.monotonic() - t, 1)
    t = time.monotonic()
    done = setexport.write_mix(spool, count, setexport.FORMATS[args.format], folder / f"mix.{setexport.FORMATS[args.format].extension}", -14.0, makeup, sr=SR)
    times["encode_s"] = round(time.monotonic() - t, 1)
    spool_gb = sum(spool.file(i).stat().st_size for i in range(count)) / 1e9
    print(json.dumps({"tracks": len(specs), "hours": round(total / SR / 3600, 2), "stems": args.stems, "format": args.format,
                      "peak_rss_mb": _rss_mb(), "spool_gb": round(spool_gb, 2),
                      "makeup_db": makeup, **times, **done["measured"], "limit_mb": 1000}))  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=2.0)
    parser.add_argument("--stems", action="store_true")
    parser.add_argument("--format", default="mp3", choices=list(setexport.FORMATS))
    parser.add_argument("--folder")
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--make", action="store_true")
    args = parser.parse_args()
    if args.child:
        child(args)
        return
    if args.make:
        for seed, spec in enumerate(_specs(args.hours)):
            _make_file(spec, seed, Path(args.folder))
        return
    folder = Path(args.folder or tempfile.mkdtemp())
    folder.mkdir(parents=True, exist_ok=True)
    # each in its own process: a child's ru_maxrss starts from its parent's, so making the tracks here would count
    cmd = [sys.executable, "-m", "tests.measure_set_export", "--hours", str(args.hours), "--format", args.format, "--folder", str(folder)]
    subprocess.run([*cmd, "--make"], check=True)
    subprocess.run([*cmd, "--child", *(["--stems"] if args.stems else [])], check=True)


if __name__ == "__main__":
    main()
