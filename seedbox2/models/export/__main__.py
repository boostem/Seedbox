"""python -m seedbox2.models.export MODEL (--out FILE | --check FILE): export from the upstream checkpoint, or check a
file, against torch; prints the parity per input and the registry fields (file, size, SHA-256)."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

# model name -> (exporter module, the checkpoint it exports)
EXPORTERS = {"beat_this": ("seedbox2.models.export.beat_this", "final0"),
             "beat_this_small": ("seedbox2.models.export.beat_this", "small0"),
             "htdemucs": ("seedbox2.models.export.htdemucs", "htdemucs"),
             "clap_audio": ("seedbox2.models.export.clap", "audio"),
             "clap_text": ("seedbox2.models.export.clap", "text")}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m seedbox2.models.export", description=__doc__)
    p.add_argument("model", choices=sorted(EXPORTERS))
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--out", type=Path, help="export the ONNX file here, then check it")
    what.add_argument("--check", type=Path, help="only check this ONNX file against torch")
    p.add_argument("--tolerance", type=float, default=None, help="max absolute difference (default: the model's, else 1e-3)")
    args = p.parse_args(argv)

    from seedbox2.models.export import common

    module, checkpoint = EXPORTERS[args.model]
    mod = importlib.import_module(module)
    path = mod.export(args.out, checkpoint) if args.out else args.check
    import onnxruntime as ort

    name = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"]).get_inputs()[0].name
    if "name" in mod.cases.__code__.co_varnames:
        cases = mod.cases(name=name)
    elif "which" in mod.cases.__code__.co_varnames:
        cases = mod.cases(checkpoint)
    else:
        cases = mod.cases()
    report = common.parity(mod.reference(checkpoint), path, cases,
                           getattr(mod, "TOLERANCE", common.TOLERANCE) if args.tolerance is None else args.tolerance,
                           transform=getattr(mod, "TRANSFORM", None))  # fmt: skip
    out = {"model": args.model, **common.registry_entry(path), "parity": report.cases, "max_error": report.max_error,
           "tolerance": report.tolerance, "ok": report.ok}
    if hasattr(mod, "front_end_report"):
        out["front_end"] = mod.front_end_report()
    print(json.dumps(out, indent=1))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
