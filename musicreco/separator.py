"""Vocal separation for musicreco.stems: a RoFormer vocal model (musicreco.roformer), downloaded on first use.

MODEL names the checkpoint, where it comes from and how to run it. Its weights are community-trained and published
without a license file, so they're fetched from their public host the first time a track is split (resumable,
size- and SHA-256-checked) into <app support>/models/, never committed or bundled. The model runs on the Apple GPU
(MPS) when there is one, with the STFT work on the CPU, else on the CPU; it's loaded once per process and dropped
by unload() (the stem queue calls it after a while without work).

Only available() is cheap: everything else imports torch (via musicreco.roformer) on first use.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SR = 44100
DOWNLOAD_CHUNK = 1 << 20
DOWNLOAD_TIMEOUT_S = 60
DOWNLOAD_TRIES = 4
_BS_BANDS = (2,) * 24 + (4,) * 12 + (12,) * 8 + (24,) * 8 + (48,) * 8 + (128, 129)

# The vocal model. BS-RoFormer, 12 layers of dim 256, one vocal mask on a stereo 2048-point STFT (hop 512);
# instrumental = track - vocals.
MODEL: dict = {
    "id": "bs-roformer-resurrection-vocals",
    "name": "BS-RoFormer Resurrection (vocals)",
    "file": "BS-Roformer-Resurrection.ckpt",
    "urls": [
        "https://huggingface.co/pcunwa/BS-Roformer-Resurrection/resolve/main/BS-Roformer-Resurrection.ckpt",
    ],
    "size": 204510749,
    "sha256": "9dbfe5cb572e4ed32a15ec727d7bd06c8d7aba97509e6fda5bc008bb1e0b2dd5",
    "arch": "bs_roformer",
    "model": {
        "dim": 256, "depth": 12, "stereo": True, "num_stems": 1, "time_transformer_depth": 1,
        "freq_transformer_depth": 1, "linear_transformer_depth": 0, "freqs_per_bands": _BS_BANDS, "dim_head": 64,
        "heads": 8, "stft_n_fft": 2048, "stft_hop_length": 512, "stft_win_length": 2048, "stft_normalized": False,
        "mask_estimator_depth": 2, "mlp_expansion_factor": 4, "skip_connection": False,
    },
    "chunk_size": 785920,  # ~17.8 s
    "num_overlap": 2,
}

_lock = threading.Lock()
_loaded: dict = {}  # {"sep": (model, device), "used": monotonic time}


class ModelUnavailable(ValueError):
    """The vocal model isn't on disk and couldn't be downloaded (offline, host down, a bad file): worth retrying later,
    unlike a song that can't be split."""


def available() -> tuple[bool, str]:
    """(can split here, why not): torch and einops importable (checked without importing them)."""
    missing = [m for m in ("torch", "einops") if importlib.util.find_spec(m) is None]
    if missing:
        return False, f"vocal splitting needs {' and '.join(missing)} (pip install -r requirements.txt)"
    return True, ""


def models_dir() -> Path:
    from .settings import app_support_dir

    d = app_support_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def model_path() -> Path:
    return models_dir() / MODEL["file"]


def _cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        from .stems import Cancelled

        raise Cancelled()


def ensure_model(progress: Callable[[float], None] | None = None, cancel: threading.Event | None = None) -> Path:
    """The checkpoint on disk, downloading it first when it's missing (a partial download resumes, a dropped
    connection is retried; ModelUnavailable when no host serves the expected file)."""
    path = model_path()
    if path.is_file() and path.stat().st_size == MODEL["size"]:
        return path
    part = path.with_name(path.name + ".part")
    errors = []
    for url in MODEL["urls"]:
        for attempt in range(DOWNLOAD_TRIES):  # dropped connections resume where they stopped
            try:
                _download(url, part, progress, cancel)
                break
            except (OSError, ValueError) as e:
                if attempt == DOWNLOAD_TRIES - 1:
                    errors.append(f"{url.split('/')[2]}: {e}")
                else:
                    time.sleep(2.0 * (attempt + 1))
        else:
            continue
        if _sha256(part) != MODEL["sha256"]:
            part.unlink(missing_ok=True)
            errors.append(f"{url.split('/')[2]}: the downloaded model doesn't match its checksum")
            continue
        os.replace(part, path)
        log.info("downloaded the vocal model %s (%.0f MB)", MODEL["file"], MODEL["size"] / 1e6)
        return path
    raise ModelUnavailable("couldn't download the vocal model (" + "; ".join(errors) + ")")


def _download(url: str, part: Path, progress: Callable[[float], None] | None, cancel: threading.Event | None) -> None:
    have = part.stat().st_size if part.is_file() else 0
    if have > MODEL["size"]:
        part.unlink()
        have = 0
    headers = {"User-Agent": "Seedbox", **({"Range": f"bytes={have}-"} if have else {})}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as r:  # noqa: S310 - fixed https URLs
        if have and r.status != 206:  # the host ignored the range: start over
            have = 0
        with open(part, "ab" if have else "wb") as f:
            done = have
            while chunk := r.read(DOWNLOAD_CHUNK):
                _cancelled(cancel)
                f.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(min(1.0, done / MODEL["size"]))
    if part.stat().st_size != MODEL["size"]:
        raise ValueError(f"incomplete download ({part.stat().st_size} of {MODEL['size']} bytes)")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(DOWNLOAD_CHUNK * 4):
            h.update(chunk)
    return h.hexdigest()


def _model(path: Path):  # noqa: ANN202
    """(network, device), loaded once (the caller holds _lock)."""
    if "sep" in _loaded:
        return _loaded["sep"]
    import torch

    from . import roformer

    threads = max(1, (os.cpu_count() or 2) - 1)  # leave a core for the app
    torch.set_num_threads(threads)
    net = (roformer.BSRoformer if MODEL["arch"] == "bs_roformer" else roformer.MelBandRoformer)(**MODEL["model"])
    roformer.load_checkpoint(net, str(path))
    device = roformer.default_device()
    net = net.float().eval().to(device)
    if device.type == "mps":
        net.spec_device = torch.device("cpu")  # complex STFT work stays off the GPU (older macOS / torch lack it)
        for mod in net.modules():
            if isinstance(mod, roformer.Attention):
                mod.max_attn_elems = int(512 * 2**20 / 4)  # cap one attention call at ~512 MB
    _loaded["sep"] = (net, device)
    log.info("vocal model loaded on %s", device)
    return _loaded["sep"]


def separate(audio: np.ndarray, sr: int = SR, progress: Callable[[float], None] | None = None,
             cancel: threading.Event | None = None) -> np.ndarray:
    """The vocal stem of (2, n) float audio at 44.1 kHz, as (2, n) float32. progress(0-1) includes a first-use
    download (the first 30%); stems.Cancelled when `cancel` is set."""
    if sr != SR:
        raise ValueError(f"the vocal model needs {SR} Hz audio")

    def report(f: float) -> None:
        if progress is not None:
            progress(max(0.0, min(1.0, f)))

    need_download = not (model_path().is_file() and model_path().stat().st_size == MODEL["size"])
    lo = 0.3 if need_download else 0.0
    path = ensure_model(lambda f: report(lo * f), cancel)
    _cancelled(cancel)
    mix = np.ascontiguousarray(np.asarray(audio, dtype=np.float32)[:2])
    if mix.shape[0] == 1:
        mix = np.repeat(mix, 2, axis=0)
    from . import roformer

    def step(done: int, total: int) -> None:
        _cancelled(cancel)
        report(lo + (1 - lo) * done / max(1, total))

    with _lock:  # one split at a time per process (the model isn't re-entrant on the GPU)
        net, device = _model(path)
        _loaded["used"] = time.monotonic()
        try:
            est = roformer.demix(net, mix, MODEL["chunk_size"], MODEL["num_overlap"], 1, step)
        except RuntimeError as e:  # an op this macOS / torch can't run on the GPU: the CPU always can
            if device.type == "cpu":
                raise
            log.warning("vocal model failed on %s (%s); running it on the CPU from now on", device, e)
            net = _to_cpu(net)
            est = roformer.demix(net, mix, MODEL["chunk_size"], MODEL["num_overlap"], 1, step)
        _loaded["used"] = time.monotonic()
    return np.ascontiguousarray(est[0], dtype=np.float32)


def _to_cpu(net):  # noqa: ANN001, ANN202
    """Move the loaded model to the CPU for good (the caller holds _lock)."""
    import torch

    net = net.to("cpu")
    net.spec_device = None
    for mod in net.modules():
        if hasattr(mod, "max_attn_elems"):
            mod.max_attn_elems = None
    _loaded["sep"] = (net, torch.device("cpu"))
    return net


def idle_s() -> float:
    """Seconds since the model last ran (inf when it isn't loaded)."""
    return time.monotonic() - _loaded["used"] if "sep" in _loaded and "used" in _loaded else float("inf")


def unload() -> None:
    """Drop the loaded model (frees its memory; the next split loads it again)."""
    with _lock:
        if _loaded.pop("sep", None) is None:
            return
        _loaded.pop("used", None)
        try:
            import gc

            import torch

            gc.collect()
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:  # noqa: BLE001 - best effort
            pass
