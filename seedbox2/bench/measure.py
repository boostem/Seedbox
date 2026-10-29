"""Run one stage in a fresh process and measure it: wall time, peak RSS of the process and its children (ffmpeg),
sampled with psutil and topped up with the OS's own peak, and, with `trace`, the peak of Python's heap (tracemalloc;
numpy reports its arrays to it). Tracing slows the stage down, so run.py times an untraced run.

A process per stage keeps one stage's memory out of the next one's numbers, the way the job system (#39) runs
stages in worker processes. Imports happen before the clock starts: `baseline_rss_mb` is the process after them.
"""

from __future__ import annotations

import importlib
import multiprocessing as mp
import sys
import time
import traceback

import psutil

MB = 1024 * 1024


def _tree_rss(proc: psutil.Process) -> int:
    try:
        procs = [proc, *proc.children(recursive=True)]
    except psutil.Error:
        return 0
    total = 0
    for p in procs:
        try:
            total += p.memory_info().rss
        except psutil.Error:
            pass
    return total


def _self_peak() -> int:
    """This process's peak RSS in bytes, as the OS counts it."""
    try:
        import resource
    except ImportError:  # Windows
        return int(getattr(psutil.Process().memory_info(), "peak_wset", 0))
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def _resolve(target: str):
    mod, _, name = target.partition(":")
    return getattr(importlib.import_module(mod), name)


def _child(conn, target: str, args: tuple, trace: bool) -> None:
    try:
        fn = _resolve(target)
        baseline = psutil.Process().memory_info().rss
        if trace:
            import tracemalloc

            tracemalloc.start()
        t0 = time.perf_counter()
        info = fn(*args)
        seconds = time.perf_counter() - t0
        py_peak = None
        if trace:
            py_peak = tracemalloc.get_traced_memory()[1] / MB
            tracemalloc.stop()
        conn.send({"seconds": seconds, "baseline_rss_mb": baseline / MB, "self_peak_mb": _self_peak() / MB,
                   "py_peak_mb": py_peak, "info": info or {}})
    except BaseException as e:  # noqa: BLE001 - reported to the parent as the stage's error
        conn.send({"error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()})
    finally:
        conn.close()


def measure(target: str, *args, trace: bool = True, interval: float = 0.02, timeout: float = 3600.0) -> dict:
    """Run `module:function`(*args) in a spawned process. {seconds, peak_rss_mb, baseline_rss_mb, py_peak_mb,
    info} with info what the function returned (a small JSON-able dict), or {error, traceback}."""
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    p = ctx.Process(target=_child, args=(child, target, args, trace), daemon=True)
    p.start()
    child.close()
    proc = psutil.Process(p.pid)
    sampled, msg, deadline = 0, None, time.monotonic() + timeout
    try:
        while msg is None:
            if parent.poll(interval):
                try:
                    msg = parent.recv()
                except EOFError:
                    break
            elif not p.is_alive() or time.monotonic() > deadline:
                break
            else:
                sampled = max(sampled, _tree_rss(proc))
    finally:
        p.join(5)
        if p.is_alive():
            p.kill()
    if msg is None:
        return {"error": "timed out" if time.monotonic() > deadline else f"the stage process died (exit {p.exitcode})"}
    if "error" in msg:
        return msg
    msg["peak_rss_mb"] = max(sampled / MB, msg.pop("self_peak_mb"))
    return msg
