"""Seedbox 2's models (#42): onnxruntime only, no torch in the app. Weights are downloaded on first run into the cache
folder, checked by SHA-256 and listed with their licences in Settings → Models.

- `registry`: `MODELS`, each a `ModelSpec` (name, version, URL, SHA-256, size, licence, attribution, fallback)
- `store`: `<cache>/models/<name>/<version>/<file>`, downloads that resume, checksums, delete
- `runtime`: `load_session(name, pool=..., pool_size=...)`: one InferenceSession per worker process, threads from the
  pool's share of the physical cores, CoreML/DirectML behind a setting with the CPU always as the fallback;
  `ModelMissing` when a model isn't there (jobs fall back to the Seedbox 1 heuristics)
- `manager`: `ModelManager`, the server's side: status, background downloads with `model.*` events, settings
- `export/` (dev-only, needs torch): reproduce the ONNX files from the upstream checkpoints, with a parity test

Analysis jobs reach a model through their JobContext: `ctx.session_or_none("beat_this")` (None: use the fallback).
"""

from seedbox2.models.registry import MODELS, ModelSpec, UnknownModel, get
from seedbox2.models.runtime import ModelMissing, intra_op_threads, load_session, open_session, providers
from seedbox2.models.store import ChecksumMismatch, DownloadCancelled, DownloadError, installed, model_path, models_dir

__all__ = ["MODELS", "ChecksumMismatch", "DownloadCancelled", "DownloadError", "ModelMissing", "ModelSpec",
           "UnknownModel", "get", "installed", "intra_op_threads", "load_session", "model_path", "models_dir",
           "open_session", "providers"]  # fmt: skip
