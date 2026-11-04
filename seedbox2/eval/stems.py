"""`python -m seedbox2.eval stems`: HTDemucs through onnxruntime against torch (#44). Dev-only: it needs torch and demucs.

A medley of MUSDB18's 7 s test clips (`--musdb DIR`, the folder of `*.stem.mp4`; the download is
https://zenodo.org/records/3270814, 150 MB) goes through `demucs.apply.apply_model` and through
`seedbox2.analysis.stems.overlap_add` with each ONNX file, with the same segments and overlap. The SI-SDR of each stem
of each clip against its true stem, averaged, must be within `--max-loss` dB of torch's.
"""

from __future__ import annotations

import itertools
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np

from seedbox2.analysis import stems
from seedbox2.analysis.stems import SAMPLE_RATE

CLIP_STREAMS = (1, 2, 3, 4)  # a .stem.mp4: the mix, then drums, bass, other, vocals: the order of stems.STEMS


def si_sdr(estimate: np.ndarray, reference: np.ndarray) -> float:
    """Scale-invariant SDR in dB, both channels as one signal."""
    e, r = estimate.ravel().astype(np.float64), reference.ravel().astype(np.float64)
    target = r * (e @ r) / (r @ r + 1e-12)
    noise = e - target
    return float(10 * np.log10((target @ target + 1e-12) / (noise @ noise + 1e-12)))


def _decode(ffmpeg: str, path: Path, stream: int) -> np.ndarray:
    pcm = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-map", f"0:{stream}", "-ac", "2", "-ar",
                          str(SAMPLE_RATE), "-f", "f32le", "-"], capture_output=True, check=True).stdout  # fmt: skip
    return np.frombuffer(pcm, np.float32).reshape(-1, 2).T


def medley(folder: Path, clips: int) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """The mix [2, n], the true stems [4, 2, n] and where each clip starts (with n at the end)."""
    from musicreco.paths import ffmpeg_path

    ffmpeg = ffmpeg_path()
    assert ffmpeg is not None, "needs ffmpeg"
    files = sorted(Path(folder).rglob("*.stem.mp4"))[:clips]
    if not files:
        raise SystemExit(f"no *.stem.mp4 in {folder}")
    mixes, truths = [], []
    for f in files:
        mix, truth = _decode(ffmpeg, f, 0), [_decode(ffmpeg, f, s) for s in CLIP_STREAMS]
        n = min(mix.shape[1], *(t.shape[1] for t in truth))
        mixes.append(mix[:, :n])
        truths.append(np.stack([t[:, :n] for t in truth]))
    edges = np.cumsum([0, *(m.shape[1] for m in mixes)]).tolist()
    return np.concatenate(mixes, 1), np.concatenate(truths, 2), edges


def scores(out: np.ndarray, truth: np.ndarray, edges: list[int]) -> np.ndarray:
    """SI-SDR [clips, stems]."""
    return np.array([[si_sdr(out[k, :, a:b], truth[k, :, a:b]) for k in range(len(stems.STEMS))]
                     for a, b in itertools.pairwise(edges)])  # fmt: skip


def run_torch(mix: np.ndarray) -> np.ndarray:
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    with torch.no_grad():
        return apply_model(get_model("htdemucs"), torch.from_numpy(mix)[None], shifts=0, split=True, overlap=stems.OVERLAP,
                           progress=False)[0].numpy()  # fmt: skip


def run_onnx(path: Path, mix: np.ndarray, threads: int) -> np.ndarray:
    from seedbox2.models.runtime import open_session

    session: Any = open_session(path, threads=threads, low_memory=True)
    chunks = (mix[:, i:i + stems.STRIDE] for i in range(0, mix.shape[1], stems.STRIDE))
    return np.concatenate(list(stems.overlap_add(chunks, lambda x: stems.separate_segment(session, x))), axis=-1)


def evaluate(musdb: Path, models: list[Path], *, clips: int = 30, threads: int = 3, max_loss: float = 0.1) -> dict:
    import torch

    torch.set_num_threads(threads)
    mix, truth, edges = medley(musdb, clips)
    t = time.monotonic()
    ref = run_torch(mix)
    report = {"clips": len(edges) - 1, "audio_seconds": mix.shape[1] / SAMPLE_RATE, "threads": threads, "max_loss": max_loss}
    base = scores(ref, truth, edges)
    report["torch"] = {"seconds": round(time.monotonic() - t, 1), "si_sdr": dict(zip(stems.STEMS, base.mean(0).round(3).tolist(), strict=True)),
                       "mean": round(float(base.mean()), 3)}  # fmt: skip
    report["models"] = {}
    for path in models:
        t = time.monotonic()
        out = run_onnx(path, mix, threads)
        seconds = time.monotonic() - t
        s = scores(out, truth, edges)
        loss = float(base.mean() - s.mean())
        report["models"][path.name] = {
            "seconds": round(seconds, 1), "mean": round(float(s.mean()), 3), "loss_db": round(loss, 3),
            "si_sdr": dict(zip(stems.STEMS, s.mean(0).round(3).tolist(), strict=True)),
            "worst_clip_stem_loss_db": round(float((base - s).max()), 2),
            "si_sdr_against_torch": dict(zip(stems.STEMS, [round(si_sdr(out[k], ref[k]), 1) for k in range(4)], strict=True)),
            "ok": abs(loss) <= max_loss,
        }  # fmt: skip
    return report


def main(args: Any) -> int:
    report = evaluate(Path(args.musdb), [Path(m) for m in args.model], clips=args.clips, threads=args.threads,
                      max_loss=args.max_loss)  # fmt: skip
    print(json.dumps(report, indent=1))
    return 0 if all(m["ok"] for m in report["models"].values()) else 1
