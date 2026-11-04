"""The models Seedbox 2 can download: what each is, where it comes from, its checksum, size and licence.

Each entry pins one file at one upstream revision, so a URL always serves the same bytes; the SHA-256 is checked after
every download. To update a model, change its `version` along with the URL, checksum and size: the new version goes
into its own folder, and the old one is removed once the new one is verified. `python -m seedbox2.models.export`
(dev-only, with torch) reproduces the ONNX files from the upstream checkpoints and prints the fields for an entry.

Licences: see "Licences" in seedbox2/README.md (#35, personal use). Keep each model's licence and credit here; the
Settings → Models page shows them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ModelSpec:
    name: str  # the key the analysis code asks for (load_session("beat_this"))
    version: str  # a new version downloads into its own folder and replaces the old one
    title: str
    file: str  # the file name in <cache>/models/<name>/<version>/
    url: str  # pinned to an upstream revision
    sha256: str
    size: int  # bytes
    licence: str  # of the weights
    attribution: str  # the credit to keep with them (and the paper to cite)
    homepage: str
    used_for: str  # what Seedbox 2 does with it
    fallback: str  # what runs without it (offline, or before the download is done)
    inputs: str = ""  # the graph's signature, for the analysis code that feeds it
    low_memory: bool = False  # open it with runtime.session_options(low_memory=True): big graphs, about half the RAM

    def to_json(self) -> dict:
        return asdict(self)


HTDEMUCS_INPUTS = ("input float32 [1, 2, 343980] (7.8 s at 44.1 kHz), x float32 [1, 4, 2048, 336] (its spectrogram: re, "
                   "im per channel) -> output [1, 4, 4, 2048, 336] (each source's spectrogram), add_67 [1, 4, 2, "
                   "343980] (each source's time branch); sources: drums, bass, other, vocals")

MODELS: dict[str, ModelSpec] = {
    spec.name: spec
    for spec in [
        ModelSpec(
            name="beat_this",
            version="final0-1",
            title="Beat This! (final0)",
            file="beat-this-final0.onnx",
            # an FP32 opset-17 export of the network (not the mel front end), with its parity results in manifest.json;
            # checked against torch by `python -m seedbox2.models.export beat_this --check`
            url="https://huggingface.co/aaatmy/beat-this-onnx/resolve/e2c0d4376fac8141905a8312c1b327849751d59c/"
                "beat-this-final0.onnx",
            sha256="e5e37b7d1802895e42559c5a1ced7619b1601d257a5c8e167c9d45de37c1b078",
            size=82_098_846,
            licence="MIT",
            attribution="Beat This! by Francesco Foscarin, Jan Schlüter and Gerhard Widmer (CPJKU, JKU Linz): "
                        "\"Beat this! Accurate beat tracking without DBN postprocessing\", ISMIR 2024. "
                        "ONNX export by aaatmy (Key & Tempo).",
            homepage="https://github.com/CPJKU/beat_this",
            used_for="Beats and downbeats",
            fallback="Seedbox 1's beat grid (musicreco.structure)",
            inputs="spectrogram float32 [1, frames, 128] (log1p(1000 x) of a 128-band slaney mel, 22,050 Hz, "
                   "n_fft 1024, hop 441, 30 Hz-11 kHz; at most 1500 frames per call) -> beat, downbeat logits "
                   "[1, frames]",
        ),
        ModelSpec(
            name="beat_this_small",
            version="small0-1",
            title="Beat This! (small0)",
            file="beat_this_small0.onnx",
            # FP32 export of the small0 network (no mel front end); its input is named `input_spectrogram`.
            # checked against torch by `python -m seedbox2.models.export beat_this_small --check` (5.5e-6)
            url="https://huggingface.co/jenila-ai/beat-this-small0-onnx/resolve/a1e1e4a99a55e167f4a7dc2df254ae404c2729ec/"
                "beat_this_small0.onnx",
            sha256="1dbbac9e2410debf083d4002f2c2b12b61a42b3cde9b0f6fb27cc9e1b61e7517",
            size=10_401_066,
            licence="MIT",
            attribution="Beat This! by Francesco Foscarin, Jan Schlüter and Gerhard Widmer (CPJKU, JKU Linz): "
                        "\"Beat this! Accurate beat tracking without DBN postprocessing\", ISMIR 2024. "
                        "ONNX export by jenila-ai.",
            homepage="https://github.com/CPJKU/beat_this",
            used_for="Beats and downbeats in low-power mode, or before final0 is downloaded",
            fallback="Beat This! final0, else Seedbox 1's beat grid",
            inputs="input_spectrogram float32 [1, frames, 128] (as beat_this) -> beat, downbeat logits [1, frames]",
        ),
        ModelSpec(
            name="htdemucs",
            version="955717e8-fp16-1",
            title="HTDemucs",
            file="htdemucs_embedded_fp16.onnx",
            # the Demucs v4 checkpoint 955717e8 with its weights in FP16; same SI-SDR as torch on MUSDB18 (#44, README),
            # checked by `python -m seedbox2.eval stems` and `python -m seedbox2.models.export htdemucs --check`
            url="https://huggingface.co/FeatherLoom/htdemucs-web/resolve/82a78ff2f7f1d7d653c5e768b2a9fbb3c78df98d/"
                "htdemucs_embedded_fp16.onnx",
            sha256="74ed9b4af89d5442bec84c4864926e6a27c58118b852bdf7ef5a8287a2df633d",
            size=94_075_835,
            licence="No licence given (upstream: likely CC BY-NC)",
            attribution="Hybrid Transformers for Music Source Separation by Simon Rouard, Francesco Yger and "
                        "Alexandre Défossez (Meta AI), ICASSP 2023; code MIT. ONNX export by FeatherLoom.",
            homepage="https://github.com/facebookresearch/demucs",
            used_for="Stems: drums, bass, other, vocals",
            fallback="No stems: EQ-only mixes, and a low-pass and a voice-band proxy in the mix metrics",
            inputs=HTDEMUCS_INPUTS,
            low_memory=True,
        ),
        ModelSpec(
            name="htdemucs_int8",
            version="955717e8-int8-matmul-1",
            title="HTDemucs (INT8, low power)",
            file="htdemucs_embedded_int8_matmul.onnx",
            # only the transformer's MatMuls are INT8: 0.02 dB below torch on MUSDB18. Quantising the convolutions too
            # loses 4.4 dB (#44, README).
            url="https://huggingface.co/7H18/htdemucs-int8/resolve/3389f1a568b0b84e92b83504b51210dda3559bcc/"
                "htdemucs_embedded_int8_matmul.onnx",
            sha256="9a61cab37904b0185589d5e8ef74d48dcdac281948855c2900439e8dbf691a61",
            size=95_566_931,
            licence="No licence given (upstream: likely CC BY-NC)",
            attribution="Hybrid Transformers for Music Source Separation by Simon Rouard, Francesco Yger and "
                        "Alexandre Défossez (Meta AI), ICASSP 2023; code MIT. INT8 export by 7H18.",
            homepage="https://github.com/facebookresearch/demucs",
            used_for="Stems in low-power mode",
            fallback="HTDemucs",
            inputs=HTDEMUCS_INPUTS,
            low_memory=True,
        ),
    ]
}


class UnknownModel(KeyError):
    """No model of that name in the registry."""


def get(name: str, models: dict[str, ModelSpec] | None = None) -> ModelSpec:
    try:
        return (MODELS if models is None else models)[name]
    except KeyError:
        raise UnknownModel(name) from None
