"""Seedbox 2's job system (#39): worker processes for the analysis and render work, out of the server process.

- `registry`: job types (name, pool, function) and the app's `REGISTRY`
- `worker`: the worker process loop and the `JobContext` a job function gets (progress, cancel, cached sessions)
- `supervisor`: `Supervisor`, the persisted priority queue, the heavy and light pools, cancel and the RAM guard
"""

from __future__ import annotations

from seedbox2.jobs.registry import POOLS, REGISTRY, JobType, Registry, UnknownJobType, register
from seedbox2.jobs.supervisor import PRIORITY, RamGuard, Supervisor
from seedbox2.jobs.worker import Cancelled, JobContext

__all__ = ["POOLS", "PRIORITY", "REGISTRY", "Cancelled", "JobContext", "JobType", "RamGuard", "Registry",
           "Supervisor", "UnknownJobType", "register"]  # fmt: skip
