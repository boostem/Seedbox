"""Dev-only: reproduce the registry's ONNX files from the upstream checkpoints and check them against torch.

Not imported by the app, and the only place in Seedbox 2 where torch is allowed. Install the extras in a separate
environment (`pip install torch beat_this onnx`) and run, for example:

    python -m seedbox2.models.export beat_this --out beat-this-final0.onnx   # export, parity test, registry fields
    python -m seedbox2.models.export beat_this --check ~/.cache/seedbox2/models/beat_this/final0-1/beat-this-final0.onnx

`--check FILE` runs the parity test on any file with the model's signature: the downloaded one, or a new upstream
export before its URL goes into the registry. Exit status 1 when the ONNX output differs from torch by more than the
tolerance. To add a model, write a module here with `export(out)`, `reference()` (the torch module) and `cases()`
(the parity inputs), and list it in `EXPORTERS` in `__main__.py`.
"""
