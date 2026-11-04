"""HTDemucs (Demucs v4, MIT; checkpoint 955717e8): the network without its STFT and iSTFT, which are `scipy.fft` in
`seedbox2.analysis.stems`, so the graph has no complex tensors.

Signature (as the registry's files): `input` float32 [1, 2, 343980], `x` float32 [1, 4, 2048, 336] -> `output` (each
source's spectrogram) [1, 4, 4, 2048, 336], `add_67` (each source's time branch) [1, 4, 2, 343980].

    pip install torch onnx demucs
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from seedbox2.models.export import common

MODEL = "htdemucs"
SEGMENT = 343_980
TOLERANCE = 2e-3  # the FP16 weights differ a little from the checkpoint; on real music that is 80 dB down (eval stems)


def reference(name: str = MODEL) -> Any:
    """The checkpoint, with `_spec` and `_ispec` replaced by the graph's inputs and outputs."""
    import torch
    from demucs.pretrained import get_model

    net = get_model(name).models[0].eval()

    class Network(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.net = net

        def forward(self, mix: torch.Tensor, spec: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
            captured = []
            self.net._spec = lambda _x: spec
            self.net._magnitude = lambda z: z
            self.net._mask = lambda _z, m: m

            def ispec(z: torch.Tensor, _length: int) -> torch.Tensor:
                captured.append(z)
                return torch.zeros((), dtype=z.dtype)

            self.net._ispec = ispec
            wave = self.net(mix)
            return captured[0], wave

    return Network().eval()


def cases(seed: int = 0) -> list[dict[str, np.ndarray]]:
    """Noise and a chord of tones, each with its own spectrogram (the graph is fixed size)."""
    from seedbox2.analysis.stems import spectrogram

    rng = np.random.default_rng(seed)
    t = np.arange(SEGMENT) / 44_100
    tones = sum(0.1 * np.sin(2 * np.pi * f * t) for f in (55.0, 440.0, 3_520.0))
    out = []
    for x in (0.1 * rng.standard_normal((2, SEGMENT)), np.stack((tones, np.roll(tones, 441)))):
        x = x.astype(np.float32)
        out.append({"input": x[None], "x": spectrogram(x)[None]})
    return out


def export(out: Path | str, name: str = MODEL) -> Path:
    import torch

    torch.backends.mha.set_fastpath_enabled(False)  # its native op has no ONNX form; the slow path computes the same
    case = cases()[0]
    example = (torch.from_numpy(case["input"]), torch.from_numpy(case["x"]))
    return common.export(reference(name), example, out, ["input", "x"], ["output", "add_67"], {}, opset=18)
