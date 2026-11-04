"""Beat This! (CPJKU, MIT): the network from the `final0` checkpoint, without its mel front end.

Signature (as the registry's file): `spectrogram` float32 [1, frames, 128] -> `beat`, `downbeat` logits [1, frames].
The front end (22,050 Hz mono, 1024-point FFT, hop 441, 128 slaney mels 30 Hz-11 kHz, log1p(1000 x)) stays in the
analysis code, which feeds at most 1500 frames (30 s) per call with overlapping windows, as upstream does.

    pip install torch onnx "beat_this @ git+https://github.com/CPJKU/beat_this"
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from seedbox2.models.export import common

CHECKPOINT = "final0"
FRAMES = (63, 128, 1264, 1500)  # parity lengths: short, a power of two, odd, the full window


class _Network:
    """Wraps BeatThis so it returns (beat, downbeat) tensors instead of a dict, and can be traced."""

    @staticmethod
    def build(checkpoint: str = CHECKPOINT) -> Any:
        import torch
        from beat_this.inference import load_model

        net = load_model(checkpoint, "cpu").eval()
        try:  # rotary embeddings cache their frequencies in a buffer, which a traced graph can't write
            from rotary_embedding_torch import RotaryEmbedding

            for m in net.modules():
                if isinstance(m, RotaryEmbedding):
                    m.cache_if_possible = False
        except ImportError:
            pass

        class Network(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.net = net

            def forward(self, spectrogram: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
                out = self.net(spectrogram)
                return out["beat"], out["downbeat"]

        return Network().eval()


def reference(checkpoint: str = CHECKPOINT) -> Any:
    return _Network.build(checkpoint)


def cases(seed: int = 0) -> list[dict[str, np.ndarray]]:
    """Log-mel-like inputs (log1p of non-negative values) of several lengths."""
    rng = np.random.default_rng(seed)
    return [{"spectrogram": np.log1p(1000 * rng.random((1, n, 128), dtype=np.float32) ** 4).astype(np.float32)}
            for n in FRAMES]  # fmt: skip


def export(out: Path | str, checkpoint: str = CHECKPOINT) -> Path:
    import torch

    module = reference(checkpoint)
    example = (torch.from_numpy(cases()[1]["spectrogram"]),)
    return common.export(module, example, out, ["spectrogram"], ["beat", "downbeat"],
                         {"spectrogram": {1: "frames"}, "beat": {1: "frames"}, "downbeat": {1: "frames"}})  # fmt: skip
