"""CLAP (#48): audio and text embeddings in one 512-dimensional space, through onnxruntime and no torch.

`laion/larger_clap_music_and_speech` (HTSAT-tiny audio encoder, RoBERTa text encoder). This module holds the two front
ends transformers would otherwise provide, both checked against it by `python -m seedbox2.models.export clap`:

- `log_mel`: 10 s of 48 kHz mono as a [1001, 64] log-mel (`ClapFeatureExtractor` with `rand_trunc`: n_fft 1024, hop 480,
  a periodic Hann window, a reflected centre, Slaney mel 50 Hz-14 kHz, power in dB). A shorter window is repeated.
- `Tokenizer`: RoBERTa's byte-level BPE, from the model's own `tokenizer.json`.

Both encoders' embeddings are normalised here: a cosine is a dot product. `seedbox2.analysis.vibe` runs them.
"""

from __future__ import annotations

import functools
import json
import math
import re
from pathlib import Path
from typing import Any

import numpy as np
from scipy import fft as sfft
import itertools

SAMPLE_RATE = 48_000
WINDOW_S = 10
WINDOW_SAMPLES = SAMPLE_RATE * WINDOW_S
N_FFT = 1024
HOP = 480
N_MELS = 64
F_MIN = 50.0
F_MAX = 14_000.0
MEL_FLOOR = 1e-10
FRAMES = WINDOW_SAMPLES // HOP + 1  # 1001
DIM = 512
MAX_TOKENS = 77


def _hz_to_mel(f: np.ndarray | float) -> np.ndarray:
    f = np.asarray(f, np.float64)
    return np.where(f < 1000, 3 * f / 200, 15 + 27 / math.log(6.4) * np.log(np.maximum(f, 1e-9) / 1000))


def _mel_to_hz(m: np.ndarray) -> np.ndarray:
    return np.where(m < 15, 200 * m / 3, 1000 * 6.4 ** ((m - 15) / 27))


@functools.lru_cache(maxsize=1)
def mel_filters() -> np.ndarray:
    """The [513, 64] Slaney filter bank."""
    fft_hz = np.linspace(0, SAMPLE_RATE / 2, N_FFT // 2 + 1)
    edges = _mel_to_hz(np.linspace(_hz_to_mel(F_MIN), _hz_to_mel(F_MAX), N_MELS + 2))
    slopes = edges[None, :] - fft_hz[:, None]
    gaps = np.diff(edges)
    down = -slopes[:, :-2] / gaps[:-1]
    up = slopes[:, 2:] / gaps[1:]
    bank = np.maximum(0.0, np.minimum(down, up))
    return (bank * (2 / (edges[2:] - edges[:-2]))).astype(np.float32)


def fit_window(x: np.ndarray) -> np.ndarray:
    """Exactly WINDOW_SAMPLES: cut, or repeated and padded with silence (the extractor's `repeatpad`)."""
    x = np.asarray(x, np.float32)
    if x.size >= WINDOW_SAMPLES:
        return x[:WINDOW_SAMPLES]
    if x.size == 0:
        return np.zeros(WINDOW_SAMPLES, np.float32)
    x = np.tile(x, WINDOW_SAMPLES // x.size)
    return np.pad(x, (0, WINDOW_SAMPLES - x.size))


def log_mel(x: np.ndarray) -> np.ndarray:
    """[FRAMES, N_MELS] float32 of one mono window at 48 kHz."""
    x = fit_window(x)
    padded = np.pad(x, N_FFT // 2, mode="reflect")
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(padded, N_FFT)[::HOP] * window
    spec = sfft.rfft(frames, axis=1, workers=1)
    power = spec.real.astype(np.float32) ** 2 + spec.imag.astype(np.float32) ** 2
    return (10 * np.log10(np.maximum(power @ mel_filters(), MEL_FLOOR))).astype(np.float32)


def normalise(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, np.float32)
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-9)


def embed_audio(session: Any, x: np.ndarray) -> np.ndarray:
    """The [512] unit vector of one mono window at 48 kHz."""
    outputs = session.run(None, {session.get_inputs()[0].name: log_mel(x)[None, None]})
    return normalise(_embeds(session, outputs)[0])


def embed_text(session: Any, tokenizer: Tokenizer, text: str) -> np.ndarray:
    ids = np.array([tokenizer.encode(text)], np.int64)
    feed = {"input_ids": ids, "attention_mask": np.ones_like(ids)}
    feed = {i.name: feed[i.name] for i in session.get_inputs()}
    return normalise(_embeds(session, session.run(None, feed))[0])


def _embeds(session: Any, outputs: list[np.ndarray]) -> np.ndarray:
    """The projected embedding, by name (the exports also give the encoder's hidden states)."""
    names = [o.name for o in session.get_outputs()]
    for i, name in enumerate(names):
        if name.endswith("embeds"):
            return outputs[i]
    raise ValueError(f"no embedding among the graph's outputs {names}")


def _bytes_to_unicode() -> dict[int, str]:
    keep = [*range(ord("!"), ord("~") + 1), *range(ord("¡"), ord("¬") + 1), *range(ord("®"), ord("ÿ") + 1)]
    out = {b: chr(b) for b in keep}
    extra = 0
    for b in range(256):
        if b not in out:
            out[b] = chr(256 + extra)
            extra += 1
    return out


_BYTES = _bytes_to_unicode()
# GPT-2's pattern with \p{L} and \p{N} as the standard library can write them
_WORDS = re.compile(r"'s|'t|'re|'ve|'m|'ll|'d| ?[^\W\d_]+| ?\d+| ?[^\s\w]+| ?_+|\s+(?!\S)|\s+")


class Tokenizer:
    """RoBERTa's tokenizer: `<s> ...BPE tokens... </s>`, at most MAX_TOKENS."""

    def __init__(self, vocab: dict[str, int], merges: list[tuple[str, str]]) -> None:
        self.vocab = vocab
        self.ranks = {pair: i for i, pair in enumerate(merges)}
        self.bos, self.eos = vocab["<s>"], vocab["</s>"]
        self._cache: dict[str, list[int]] = {}

    @classmethod
    def load(cls, path: Path | str) -> Tokenizer:
        model = json.loads(Path(path).read_text(encoding="utf-8"))["model"]
        merges = [tuple(m.split(" ", 1)) if isinstance(m, str) else tuple(m) for m in model["merges"]]
        return cls(model["vocab"], merges)

    def _bpe(self, token: str) -> list[int]:
        if (known := self._cache.get(token)) is not None:
            return known
        word = list(token)
        while len(word) > 1:
            pair = min(itertools.pairwise(word), key=lambda p: self.ranks.get(p, math.inf))
            if pair not in self.ranks:
                break
            merged: list[str] = []
            i = 0
            while i < len(word):
                if i + 1 < len(word) and (word[i], word[i + 1]) == pair:
                    merged.append(word[i] + word[i + 1])
                    i += 2
                else:
                    merged.append(word[i])
                    i += 1
            word = merged
        unk = self.vocab["<unk>"]
        ids = [self.vocab.get(w, unk) for w in word]
        self._cache[token] = ids
        return ids

    def encode(self, text: str) -> list[int]:
        ids: list[int] = []
        for piece in _WORDS.findall(text):
            ids += self._bpe("".join(_BYTES[b] for b in piece.encode("utf-8")))
        return [self.bos, *ids[: MAX_TOKENS - 2], self.eos]
