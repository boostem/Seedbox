"""Shared by the exporters: torch -> ONNX, the parity test against torch and the registry fields of a file."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

OPSET = 17
TOLERANCE = 1e-3  # max absolute difference between the ONNX and torch outputs (logits)


def export(module: Any, example: tuple, out: Path | str, inputs: Sequence[str], outputs: Sequence[str],
           dynamic_axes: dict[str, dict[int, str]], opset: int = OPSET) -> Path:  # fmt: skip
    """Export a torch module to a single ONNX file (weights inside), with `dynamic_axes` left free."""
    import torch

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with torch.no_grad():
        torch.onnx.export(module, example, str(out), input_names=list(inputs), output_names=list(outputs),
                          dynamic_axes={**dynamic_axes, **{o: {} for o in outputs if o not in dynamic_axes}},
                          opset_version=opset, do_constant_folding=True, dynamo=False)  # fmt: skip
    _inline_external_data(out)
    return out


def _inline_external_data(path: Path) -> None:
    """One self-contained file, whatever the exporter did with large weights."""
    try:
        import onnx
    except ImportError:
        return
    model = onnx.load(str(path), load_external_data=True)
    onnx.save_model(model, str(path), save_as_external_data=False)
    for extra in path.parent.glob(path.name + ".data"):
        extra.unlink()


@dataclass
class Parity:
    tolerance: float
    cases: list[dict] = field(default_factory=list)  # {input: "1x63x128", max_error} per case

    @property
    def max_error(self) -> float:
        return max((c["max_error"] for c in self.cases), default=0.0)

    @property
    def ok(self) -> bool:
        return bool(self.cases) and self.max_error <= self.tolerance


def _as_list(out: Any) -> list[np.ndarray]:
    if isinstance(out, dict):
        out = list(out.values())
    if not isinstance(out, (list, tuple)):
        out = [out]
    return [o.detach().cpu().numpy() if hasattr(o, "detach") else np.asarray(o) for o in out]


def parity(module: Any, onnx_path: Path | str, cases: Sequence[dict[str, np.ndarray]],
           tolerance: float = TOLERANCE, transform: Any = None) -> Parity:  # fmt: skip
    """Run each case (input name -> array) through torch and onnxruntime (CPU) and compare every output, after
    `transform` if there is one."""
    import onnxruntime as ort
    import torch

    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    report = Parity(tolerance)
    with torch.no_grad():
        for case in cases:
            want = _as_list(module(*(torch.from_numpy(v) for v in case.values())))
            got = session.run(None, case)
            if transform is not None:
                want, got = [transform(w) for w in want], [transform(g) for g in got]
            if len(got) != len(want):
                raise ValueError(f"ONNX has {len(got)} outputs, torch {len(want)}")
            err = max(float(np.max(np.abs(g.astype(np.float64) - w.astype(np.float64)))) if g.size else 0.0
                      for g, w in zip(got, want, strict=True))  # fmt: skip
            shapes = ", ".join("x".join(map(str, v.shape)) for v in case.values())
            report.cases.append({"input": shapes, "max_error": err})
    return report


def registry_entry(path: Path | str) -> dict:
    """The file's fields for a ModelSpec: file name, size and SHA-256."""
    path = Path(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return {"file": path.name, "size": path.stat().st_size, "sha256": h.hexdigest()}
