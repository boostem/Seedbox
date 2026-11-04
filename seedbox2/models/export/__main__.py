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
             "beat_this_small": ("seedbox2.models.export.beat_this", "small0")}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m seedbox2.models.export", description=__doc__)
    p.add_argument("model", choices=sorted(EXPORTERS))
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--out", type=Path, help="export the ONNX file here, then check it")
    what.add_argument("--check", type=Path, help="only check this ONNX file against torch")
    p.add_argument("--tolerance", type=float, default=None, help="max absolute difference (default 1e-3)")
    args = p.parse_args(argv)

    from seedbox2.models.export import common

    module, checkpoint = EXPORTERS[args.model]
    mod = importlib.import_module(module)
    path = mod.export(args.out, checkpoint) if args.out else args.check
    import onnxruntime as ort

    name = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"]).get_inputs()[0].name
    report = common.parity(mod.reference(checkpoint), path, mod.cases(name=name),
                           common.TOLERANCE if args.tolerance is None else args.tolerance)  # fmt: skip
    out = {"model": args.model, **common.registry_entry(path), "parity": report.cases, "max_error": report.max_error,
           "tolerance": report.tolerance, "ok": report.ok}
    print(json.dumps(out, indent=1))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
