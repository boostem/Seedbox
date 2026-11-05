"""A tiny ONNX model for `--self-test`, embedded so that it needs no download and no data file in the frozen app.

It is y = Relu(x · [[1, 2], [-3, 4]]), opset 17, the opset of our exports. Running it shows that onnxruntime's native
library loads and runs a graph, in this process and in a spawned job worker (the job system of #39 spawns them in the frozen app too).
"""

from __future__ import annotations

import base64
import multiprocessing
import sys
from concurrent.futures import ProcessPoolExecutor
from typing import Any

TINY_MODEL = base64.b64decode(
    "CAgSCHNlZWRib3gyOnIKEQoBeAoBdxIBbSIGTWF0TXVsCgwKAW0SAXkiBFJlbHUSCHNlbGZ0ZXN0KhsIAggCEAFCAXdKEAAAgD8AAABAAABAwAAAgEBaEwoBeBIOCgwIARIICgIIAQoCCAJiEwoBeRIOCgwIARIICgIIAQoCCAJCBAoAEBE="
)
INPUT = [[1.0, 1.0]]
EXPECTED = [[0.0, 6.0]]
WORKER_TIMEOUT_S = 120.0


def run_tiny_model() -> list[list[float]]:
    """The tiny model's output for INPUT, through the same session options and providers as the real models."""
    import numpy as np
    import onnxruntime as ort

    from seedbox2.models import runtime

    session = ort.InferenceSession(TINY_MODEL, sess_options=runtime.session_options(1),
                                   providers=runtime.providers())  # fmt: skip
    return session.run(["y"], {"x": np.array(INPUT, dtype=np.float32)})[0].tolist()


def check() -> dict[str, Any]:
    """Run the model here and in a spawned worker: {ok, version, providers, in_process, worker}, or {ok, error}."""
    try:
        import onnxruntime as ort

        report: dict[str, Any] = {"version": ort.__version__, "providers": ort.get_available_providers()}
        report["in_process"] = run_tiny_model()
        spawn = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=1, mp_context=spawn) as pool:
            report["worker"] = pool.submit(run_tiny_model).result(timeout=WORKER_TIMEOUT_S)
        report["ok"] = report["in_process"] == EXPECTED and report["worker"] == EXPECTED
    except Exception as e:  # noqa: BLE001 - the report says what went wrong
        print(f"onnxruntime self-test failed: {e}", file=sys.stderr)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return report
