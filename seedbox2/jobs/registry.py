"""Job types: a name, the pool that runs it and the function a worker process calls.

A job function takes `(payload, ctx)` (payload: the JSON dict it was submitted with; ctx: `worker.JobContext`) and
returns a JSON-encodable result (or None). It must be a module-level function: worker processes are spawned, so they
import it by its `module:qualname` rather than unpickling it.

    from seedbox2.jobs import register

    @register("beats", pool="light")
    def beats(payload, ctx):
        model = ctx.cached("beat_this", load_model)  # one ONNX session per worker process, reused across jobs
        ...
        ctx.progress(0.5)                           # also raises Cancelled once the job is cancelled
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

POOLS = ("heavy", "light")


class UnknownJobType(KeyError):
    pass


@dataclass(frozen=True)
class JobType:
    name: str
    pool: str  # heavy: stems, full renders (RAM-heavy, one at a time); light: beats, key, waveforms, embeddings, plans
    target: str  # "module:qualname" of the job function
    resumable: bool = True  # re-run from the start after a restart that interrupted it (else: marked failed)


def target_of(fn: Callable[..., Any]) -> str:
    if "<" in fn.__qualname__:  # a lambda or a function defined inside another one
        raise ValueError(f"job functions must be module-level functions, not {fn.__qualname__}")
    return f"{fn.__module__}:{fn.__qualname__}"


def resolve(target: str) -> Callable[..., Any]:
    module, _, qualname = target.partition(":")
    obj: Any = importlib.import_module(module)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


class Registry:
    def __init__(self) -> None:
        self._types: dict[str, JobType] = {}

    def add(self, name: str, fn: Callable[..., Any] | str, *, pool: str = "light", resumable: bool = True) -> JobType:
        if pool not in POOLS:
            raise ValueError(f"pool must be one of {POOLS}")
        job_type = JobType(name, pool, fn if isinstance(fn, str) else target_of(fn), resumable)
        self._types[name] = job_type
        return job_type

    def register(self, name: str, *, pool: str = "light", resumable: bool = True) -> Callable[[Callable], Callable]:
        """Decorator form of `add`."""

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.add(name, fn, pool=pool, resumable=resumable)
            return fn

        return deco

    def get(self, name: str) -> JobType:
        try:
            return self._types[name]
        except KeyError:
            raise UnknownJobType(name) from None

    def __contains__(self, name: object) -> bool:
        return name in self._types

    def names(self) -> list[str]:
        return sorted(self._types)


REGISTRY = Registry()  # the app's job types; each analysis module registers its own on import
register = REGISTRY.register
