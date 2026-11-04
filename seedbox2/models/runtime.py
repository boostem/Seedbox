"""onnxruntime sessions: one per model per worker process, sized to the worker's share of the CPU.

- Threads: a worker's intra-op threads are the physical cores of its pool's share divided between the pool's workers
  (`intra_op_threads`); inter-op is 1 (the graphs run sequentially, and parallel branches would only oversubscribe
  the cores the other workers use). The light pool runs cores - 2 workers side by side, so each of its sessions gets
  one thread on most laptops; the one heavy worker (stems) gets half the physical cores.
- Execution providers: CoreML on macOS and DirectML on Windows when the `models.accelerator` setting is on (off by
  default: the CPU is predictable on the reference laptops), always with the CPU after them, and the CPU alone if a
  session can't be made with the accelerator.
- Memory: a `low_memory` model runs without onnxruntime's buffer reuse and arena. The planner keeps every buffer it
  may reuse later alive until then, which took HTDemucs from 1.25 GB to 2.5 GB (5.2 GB with the arena) for the same
  speed.
- A model that isn't downloaded raises ModelMissing; jobs call `ctx.session_or_none(name)` and fall back to the
  Seedbox 1 heuristics instead.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from seedbox2.models import store
from seedbox2.models.registry import MODELS, ModelSpec, get

log = logging.getLogger(__name__)

CPU = "CPUExecutionProvider"
ACCELERATORS = {"darwin": "CoreMLExecutionProvider", "win32": "DmlExecutionProvider"}
# the fraction of the physical cores each pool's workers share between them
POOL_SHARE = {"light": 1.0, "heavy": 0.5}


class ModelMissing(Exception):  # noqa: N818 - a state, not a failure: the caller falls back
    """The model isn't downloaded (offline, or the download hasn't finished)."""

    def __init__(self, spec: ModelSpec) -> None:
        super().__init__(f"{spec.title} isn't downloaded; using {spec.fallback} instead")
        self.spec = spec


def physical_cores() -> int:
    try:
        import psutil

        n = psutil.cpu_count(logical=False)
    except Exception:  # noqa: BLE001 - psutil can't tell on some systems
        n = None
    return max(1, n or os.cpu_count() or 1)


def intra_op_threads(pool: str = "light", pool_size: int = 1, cores: int | None = None) -> int:
    """Threads for one session in one of the pool's `pool_size` workers: its share of the physical cores."""
    cores = physical_cores() if cores is None else cores
    share = cores * POOL_SHARE.get(pool, 1.0)
    return max(1, int(share // max(1, pool_size)))


def providers(accelerator: bool = False, platform: str = sys.platform,
              available: Sequence[str] | None = None) -> list[str]:  # fmt: skip
    """The execution providers to ask for, best first; the CPU is always there."""
    if available is None:
        import onnxruntime as ort

        available = ort.get_available_providers()
    chosen = []
    accel = ACCELERATORS.get(platform)
    if accelerator and accel and accel in available:
        chosen.append(accel)
    chosen.append(CPU)
    return chosen


def session_options(threads: int, low_memory: bool = False) -> Any:
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = max(1, int(threads))
    opts.inter_op_num_threads = 1
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    if low_memory:
        opts.enable_mem_reuse = False
        opts.enable_mem_pattern = False
        opts.enable_cpu_mem_arena = False
    return opts


def open_session(path: Path | str, *, threads: int = 1, accelerator: bool = False, platform: str = sys.platform,
                 low_memory: bool = False) -> Any:  # fmt: skip
    """An InferenceSession on `path`. With the accelerator, falls back to the CPU if that session fails."""
    import onnxruntime as ort

    wanted = providers(accelerator, platform)
    try:
        return ort.InferenceSession(str(path), sess_options=session_options(threads, low_memory), providers=wanted)
    except Exception as e:  # an accelerator that can't take this graph
        if wanted == [CPU]:
            raise
        log.warning("couldn't open %s with %s (%s); using the CPU", path, wanted[0], e)
        return ort.InferenceSession(str(path), sess_options=session_options(threads, low_memory), providers=[CPU])


def load_session(name: str, *, root: Path | None = None, pool: str = "light", pool_size: int = 1,
                 accelerator: bool = False, models: dict[str, ModelSpec] | None = None) -> Any:  # fmt: skip
    """The model's session, sized for one worker of `pool`. ModelMissing if it isn't downloaded."""
    spec = get(name, MODELS if models is None else models)
    root = store.models_dir() if root is None else Path(root)
    if not store.installed(spec, root):
        raise ModelMissing(spec)
    threads = intra_op_threads(pool, pool_size)
    session = open_session(store.model_path(spec, root), threads=threads, accelerator=accelerator,
                           low_memory=spec.low_memory)
    log.info("loaded %s %s (%d intra-op threads, %s)", name, spec.version, threads, session.get_providers()[0])
    return session
