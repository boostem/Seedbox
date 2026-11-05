"""CLAP (LAION, Apache-2.0): `larger_clap_music_and_speech`'s two encoders with their projections, as the registry's
files give them.

Signature: `input_features` float32 [batch, 1, 1001, 64] -> `audio_embeds` [batch, 512]; `input_ids` int64 [batch, tokens]
-> `text_embeds` [batch, 512]. The front ends stay in `seedbox2.analysis.clap`; `front_end_report` compares them with
transformers' and is printed by `--check`, along with the parity of the graphs.

    pip install torch transformers onnx
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from seedbox2.models.export import common

HUB = "laion/larger_clap_music_and_speech"
TOLERANCE = 5e-3  # per component of the unit vectors; FP16 weights keep the cosine above 0.9999
TEXTS = ("warm", "dark", "vocal house", "euphoric sunset techno", "Déjà vu, it's 90's R&B!!", "deep_house 124bpm")


def TRANSFORM(v: np.ndarray) -> np.ndarray:
    """transformers normalises its embeddings and the registry's files don't: only the direction counts."""
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-9)


def _embed(out: Any) -> Any:
    return getattr(out, "pooler_output", out)


class _Audio:
    @staticmethod
    def build() -> Any:
        import torch
        from transformers import ClapModel

        class Audio(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.model = ClapModel.from_pretrained(HUB).eval()

            def forward(self, input_features: Any) -> Any:
                return _embed(self.model.get_audio_features(input_features=input_features))

        return Audio().eval()


class _Text:
    @staticmethod
    def build() -> Any:
        import torch
        from transformers import ClapModel

        class Text(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.model = ClapModel.from_pretrained(HUB).eval()

            def forward(self, input_ids: Any) -> Any:
                return _embed(self.model.get_text_features(input_ids=input_ids, attention_mask=input_ids * 0 + 1))

        return Text().eval()


def reference(which: str = "audio") -> Any:
    return (_Audio if which == "audio" else _Text).build()


def _noise(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(480_000) / 48_000
    x = 0.3 * np.sin(2 * np.pi * (60 + 40 * seed) * t) + 0.1 * rng.standard_normal(t.size)
    return (x * (np.sin(2 * np.pi * 2 * t) > 0)).astype(np.float32)


def cases(which: str = "audio") -> list[dict[str, np.ndarray]]:
    from seedbox2.analysis import clap

    if which == "audio":
        return [{"input_features": clap.log_mel(_noise(s))[None, None]} for s in range(3)]
    from transformers import ClapProcessor

    tokenizer = ClapProcessor.from_pretrained(HUB).tokenizer
    return [{"input_ids": np.array([tokenizer(t)["input_ids"]], np.int64)} for t in TEXTS]


def export(out: Path | str, which: str = "audio") -> Path:
    """Reproduces a file: FP32 here; the registry's are its FP16 equivalents. onnxruntime's float16 converter gets
    from one to the other."""
    module = reference(which)
    example = cases(which)[0]
    import torch

    tensors = tuple(torch.from_numpy(v) for v in example.values())
    name = next(iter(example))
    output = "audio_embeds" if which == "audio" else "text_embeds"
    return common.export(module, tensors, out, [name], [output],
                         {name: {0: "batch", **({} if which == "audio" else {1: "tokens"})}, output: {0: "batch"}})  # fmt: skip


def front_end_report() -> dict:
    """seedbox2's log-mel (largest difference, dB) and tokens (equal or not) against transformers'."""
    from transformers import ClapProcessor

    from seedbox2.analysis import clap

    processor = ClapProcessor.from_pretrained(HUB)
    err = 0.0
    for s in range(3):
        x = _noise(s)
        want = processor.feature_extractor(x, sampling_rate=clap.SAMPLE_RATE, return_tensors="np")["input_features"][0, 0]
        err = max(err, float(np.abs(want - clap.log_mel(x)).max()))
    tokenizer_file = Path(processor.tokenizer.save_pretrained(_tmp())[0]).parent / "tokenizer.json"
    ours = clap.Tokenizer.load(tokenizer_file)
    same = all(ours.encode(t) == processor.tokenizer(t)["input_ids"] for t in TEXTS)
    return {"log_mel_max_db": err, "tokens_equal": same}


def _tmp() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="clap-tokenizer-")
