"""The benchmark corpus: 10 royalty-free tracks (Kevin MacLeod, incompetech.com, CC BY 4.0), downloaded once into
the cache folder and checked against their SHA-256; or a synthetic corpus (`--synthetic`) for CI and offline runs.

Changing the list changes every number: bump CORPUS when you do, so `compare` doesn't compare different corpora.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

CORPUS = "incompetech-10-v1"
BASE_URL = "https://incompetech.com/music/royalty-free/mp3-royaltyfree/"
LICENSE = "Kevin MacLeod (incompetech.com), Creative Commons: By Attribution 4.0 (https://creativecommons.org/licenses/by/4.0/)"


@dataclass(frozen=True)
class Track:
    name: str
    sha256: str
    duration_s: float

    @property
    def url(self) -> str:
        return BASE_URL + urllib.parse.quote(f"{self.name}.mp3")

    @property
    def filename(self) -> str:
        return f"{self.name}.mp3"


TRACKS: tuple[Track, ...] = (
    Track("Funkorama", "6be229815d85a0b99382b944d31098b882702c04ac7ad899e44ab7f8ba89b5ad", 201.5),
    Track("Electro Cabello", "f304151035c2647de56dbf1995dcc90f3c942c656edfe7516541e7b76e4044ef", 190.8),
    Track("Equatorial Complex", "b31de67f7422c0a48eb9faf16f3d2379dd568f9b37f275c537d320194e327f2c", 240.1),
    Track("Pixelland", "889fd6f06fd077689d512d4169a0efc4a61b717af73ac64ecca361eb5a4c4b7d", 233.8),
    Track("Ouroboros", "c9ed09f7086eb9a084a080a2fc2ad4c164db78fd4078e21c3a5f49cd615a1df6", 161.6),
    Track("Voltaic", "98d6a79a8577c17648ffa533ab213f709005c3a3a51c32e2ad8bd38eb361b5df", 196.1),
    Track("Exit the Premises", "441d1b594a15b0acdac531495e9c56906e3df5d4002fc651b5a0b763f679c70b", 210.1),
    Track("Cyborg Ninja", "b266d2ac6af6b295b8ac278dafc63cb6c336551b7cc3c66e824f9c1bda945f7a", 180.1),
    Track("Hyperfun", "ac3dcc688f183717cf3001a3250c2625ccaa2e97676aa9ab5cb3a8583b8611e1", 233.3),
    Track("Raving Energy", "0aa9c5fc963d458a75df8760d67b5709465ba8badccd2cd356310ea856250b50", 267.6),
)


def default_dir() -> Path:
    """$SEEDBOX_BENCH_DIR, else bench/corpus in Seedbox 2's cache folder."""
    if os.environ.get("SEEDBOX_BENCH_DIR", "").strip():
        return Path(os.environ["SEEDBOX_BENCH_DIR"]) / "corpus"
    from seedbox2.app import data_dirs

    cache = os.environ.get("SEEDBOX_CACHE_DIR", "").strip() or data_dirs()["SEEDBOX_CACHE_DIR"]
    return Path(cache) / "bench" / "corpus"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(folder: Path, tracks: tuple[Track, ...] = TRACKS, log=print) -> list[tuple[str, Path, float]]:
    """(name, path, duration) of each track in `folder`, downloading the ones that are missing or don't match."""
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for t in tracks:
        path = folder / t.filename
        if not path.exists() or _sha256(path) != t.sha256:
            log(f"downloading {t.name} ...")
            part = path.with_suffix(".part")
            req = urllib.request.Request(t.url, headers={"User-Agent": "seedbox2-bench"})
            with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as f:
                shutil.copyfileobj(r, f)
            got = _sha256(part)
            if got != t.sha256:
                part.unlink()
                raise RuntimeError(f"{t.name}: SHA-256 {got} doesn't match {t.sha256} ({t.url} changed?)")
            part.replace(path)
        out.append((t.name, path, t.duration_s))
    return out


SR = 44100


def synth_track(bpm: float, seconds: float, seed: int, sr: int = SR) -> np.ndarray:
    """Stereo float32 four-to-the-floor loop: kick on the beats, hats on the off-beats, a bass line per bar, and a
    drum-only intro and outro of 8 bars, so the analyser finds a grid and sections."""
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    y = np.zeros(n, np.float32)
    beat = 60.0 / bpm
    kick_t = np.arange(int(0.25 * sr)) / sr
    kick = (np.sin(2 * np.pi * (50 + 100 * np.exp(-kick_t * 30)) * kick_t) * np.exp(-kick_t * 12)).astype(np.float32)
    hat = (rng.standard_normal(int(0.04 * sr)) * np.exp(-np.arange(int(0.04 * sr)) / sr * 90)).astype(np.float32)
    notes = 55.0 * 2 ** (rng.integers(0, 12, 4) / 12)
    bars = int(seconds / (4 * beat))
    for k in range(int(seconds / beat)):
        i = int(k * beat * sr)
        for snd, at, gain in ((kick, i, 0.8), (hat, i + int(beat * sr / 2), 0.15)):
            m = min(len(snd), n - at)
            if m > 0:
                y[at:at + m] += gain * snd[:m]
        bar = k // 4
        if 8 <= bar < bars - 8:
            m = min(int(beat * sr), n - i)
            t = np.arange(m) / sr
            y[i:i + m] += (0.25 * np.sin(2 * np.pi * notes[bar % 4] * t) * np.exp(-t * 3)).astype(np.float32)
    y /= max(1e-9, float(np.abs(y).max())) / 0.8
    return np.stack([y, y])


def synthetic(folder: Path, count: int = 10, seconds: float = 120.0) -> list[tuple[str, Path, float]]:
    """`count` synthetic WAVs of `seconds` each (118–132 BPM), written once into `folder`."""
    from scipy.io import wavfile

    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(count):
        bpm = 118.0 + (i * 7) % 15
        name = f"synth-{i:02d}-{bpm:g}bpm-{seconds:g}s"
        path = folder / f"{name}.wav"
        if not path.exists():
            part = folder / f"{name}.part.wav"
            wavfile.write(part, SR, np.ascontiguousarray(synth_track(bpm, seconds, i).T))
            part.replace(path)
        out.append((name, path, seconds))
    return out
