"""`python -m seedbox2.bench frontend`: frame rate and long tasks of frontend2 in headless Chrome.

Serves frontend2/dist with Seedbox 2's own server, opens `#/bench` (pages/BenchPage.tsx) in headless Chrome and drives
it over the DevTools protocol: ⌘/ctrl-wheel zooming, horizontal scrolling, moving the mouse over the waveform (its
time readout), playback with the zoomed waveform following the playhead, and scrolling a 10,000-row virtualised table.
During each scenario the page counts requestAnimationFrame callbacks (fps, frame times), long tasks
(PerformanceObserver, > 50 ms) and React commits (a stub of React DevTools' hook, installed before the page loads:
frontend2/PERFORMANCE.md's rule is that the waveform scenarios cause next to none). `--cpu-throttle 4` slows the CPU
down like DevTools does, to approximate a slow laptop on a fast machine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import BinaryIO

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "frontend2" / "dist"

# Installed before the page's scripts run: React DOM reports every commit to DevTools' global hook when one exists.
COMMITS_JS = """
window.__benchCommits = 0;
window.__REACT_DEVTOOLS_GLOBAL_HOOK__ = {
  supportsFiber: true, isDisabled: false, renderers: new Map(),
  inject(renderer) { this.renderers.set(this.renderers.size + 1, renderer); return this.renderers.size; },
  onCommitFiberRoot() { window.__benchCommits++; },
  onCommitFiberUnmount() {}, onPostCommitFiberRoot() {}, onScheduleFiberRoot() {}, setStrictMode() {}, checkDCE() {},
};
"""

# Runs in the page: rAF timestamps, long tasks and React commits between start() and stop().
METER_JS = """
window.__benchMeter ??= (() => {
  const s = { frames: [], tasks: [], on: false, t0: 0, c0: 0 };
  new PerformanceObserver((list) => {
    if (s.on) for (const e of list.getEntries()) s.tasks.push(e.duration);
  }).observe({ type: "longtask" });
  const tick = (t) => { if (s.on) s.frames.push(t); requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
  return {
    start() { s.frames = []; s.tasks = []; s.on = true; s.t0 = performance.now(); s.c0 = window.__benchCommits ?? 0; },
    stop() {
      s.on = false;
      const seconds = (performance.now() - s.t0) / 1000, commits = (window.__benchCommits ?? 0) - s.c0;
      const f = s.frames, d = [];
      for (let i = 1; i < f.length; i++) d.push(f[i] - f[i - 1]);
      const mean = d.length ? d.reduce((a, b) => a + b, 0) / d.length : 0;
      const stddev = d.length ? Math.sqrt(d.reduce((a, b) => a + (b - mean) ** 2, 0) / d.length) : 0;
      d.sort((a, b) => a - b);
      const span = f.length > 1 ? (f[f.length - 1] - f[0]) / 1000 : 0;
      return {
        seconds,
        frames: f.length,
        fps: span ? (f.length - 1) / span : 0,
        frame_stddev_ms: stddev,
        frame_p95_ms: d.length ? d[Math.floor(0.95 * (d.length - 1))] : 0,
        frame_max_ms: d.length ? d[d.length - 1] : 0,
        long_tasks: s.tasks.length,
        long_task_ms_max: s.tasks.length ? Math.max(...s.tasks) : 0,
        react_commits: commits,
        react_commits_per_s: seconds ? commits / seconds : 0,
      };
    },
  };
})();
true
"""

CANDIDATES = {
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "/Applications/Chromium.app/Contents/MacOS/Chromium",
               "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
    "win32": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"],
}  # fmt: skip


def find_chrome() -> str | None:
    """$CHROME, else Chrome, Chromium or Edge from PATH or their usual install folders."""
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge"):
        if path := shutil.which(name):
            return path
    return next((p for p in CANDIDATES.get(sys.platform, []) if Path(p).exists()), None)


class Cdp:
    """A minimal DevTools protocol client on one page target."""

    def __init__(self, ws):
        self.ws = ws  # a websockets.sync.client connection
        self.next_id = 0

    def call(self, method: str, timeout: float = 60.0, **params) -> dict:
        self.next_id += 1
        my = self.next_id
        self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout))
            if msg.get("id") == my:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})

    def eval(self, expression: str, timeout: float = 60.0):
        r = self.call("Runtime.evaluate", timeout, expression=expression, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RuntimeError(d.get("exception", {}).get("description") or d.get("text"))
        return r["result"].get("value")


def _wait(pred, timeout: float, what: str, step: float = 0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if (v := pred()) is not None and v is not False:
            return v
        time.sleep(step)
    raise TimeoutError(f"timed out waiting for {what}")


def _launch(chrome: str, profile: Path, size: tuple[int, int], software_gl: bool = False) -> tuple[subprocess.Popen, BinaryIO, str]:
    args = [chrome, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required", "--mute-audio",
            f"--window-size={size[0]},{size[1]}", "about:blank"]  # fmt: skip
    if software_gl:  # WebGL on the CPU: slower than any integrated GPU, so a floor for the waveform renderer
        args[1:1] = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
    if os.environ.get("CI") and sys.platform.startswith("linux"):
        args.insert(1, "--no-sandbox")  # GitHub's Ubuntu runners don't allow Chrome's user-namespace sandbox
    env = dict(os.environ)
    if os.name == "posix" and len(tempfile.gettempdir()) > 60:
        env["TMPDIR"] = "/tmp"  # Chrome's singleton socket path must fit in 108 bytes
    errors = open(profile / "chrome-stderr.log", "w+b")  # closed by run()
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=errors, env=env)
    port_file = profile / "DevToolsActivePort"

    def port() -> str | None:
        if proc.poll() is not None:
            errors.seek(0)
            tail = errors.read().decode(errors="replace").strip().splitlines()[-3:]
            raise RuntimeError(f"Chrome exited ({proc.returncode}): {' / '.join(tail)}")
        return port_file.exists() and port_file.read_text().split("\n")[0].strip() or None

    def page(port: str) -> str | None:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
            return next((t["webSocketDebuggerUrl"] for t in json.load(r) if t.get("type") == "page"), None)

    try:
        devtools = _wait(port, 30, "Chrome's DevTools port")
        return proc, errors, _wait(lambda: page(devtools), 30, "a page target")
    except BaseException:
        _stop(proc)
        errors.close()
        raise


def _wheel(cdp: Cdp, x: float, y: float, dx: float = 0.0, dy: float = 0.0, ctrl: bool = False) -> None:
    cdp.call("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=dx, deltaY=dy, modifiers=2 if ctrl else 0)


def _rect(cdp: Cdp, selector: str) -> list[float]:
    return cdp.eval(f"(() => {{ const r = document.querySelector({json.dumps(selector)}).getBoundingClientRect();"
                    " return [r.left, r.top, r.width, r.height]; })()")  # fmt: skip


def _measure(cdp: Cdp, body) -> dict:
    cdp.eval("window.__benchMeter.start()")
    body()
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in cdp.eval("window.__benchMeter.stop()").items()}


def now_playing(cdp: Cdp, url: str, seconds: float) -> dict[str, dict]:
    """The full-screen decks (#77) on their demo: both waveforms scrolling, meters and knobs moving, then at low power's cap."""
    cdp.call("Page.navigate", url=f"{url.rstrip('/')}/#/now-playing?demo=1")
    _wait(lambda: cdp.eval("!!window.__seedboxNowPlaying && !!document.querySelector('[data-bench=now-playing] canvas')"),
          30, "the #/now-playing page")  # fmt: skip
    time.sleep(0.5)

    def run() -> dict:
        start = cdp.eval("window.__seedboxNowPlaying.draws()")
        out = _measure(cdp, lambda: time.sleep(seconds))
        out["draws_per_s"] = round((cdp.eval("window.__seedboxNowPlaying.draws()") - start) / out["seconds"], 2)
        return out

    cdp.eval("window.__seedboxNowPlaying.run(true)")
    time.sleep(0.5)
    out = {"now_playing": run()}
    cdp.eval("window.__seedboxNowPlaying.frameCap(30)")
    out["now_playing_low_power"] = run()
    cdp.eval("window.__seedboxNowPlaying.frameCap(null)")
    cdp.eval("window.__seedboxNowPlaying.run(false)")
    return out


def scenarios(cdp: Cdp, seconds: float) -> dict[str, dict]:
    rect = _rect(cdp, "[data-bench=waveform] canvas")
    x, y = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
    out = {}

    def zoom() -> None:
        end, k = time.monotonic() + seconds, 0
        while time.monotonic() < end:
            _wheel(cdp, x, y, dy=-30 if (k // 30) % 2 == 0 else 30, ctrl=True)  # 30 steps in, 30 out
            k += 1
            time.sleep(1 / 120)

    def scroll() -> None:
        end, k = time.monotonic() + seconds, 0
        while time.monotonic() < end:
            _wheel(cdp, x, y, dx=40 if (k // 60) % 2 == 0 else -40)
            k += 1
            time.sleep(1 / 120)

    out["zoom"] = _measure(cdp, zoom)
    for _ in range(40):  # zoomed in, so scrolling and the playhead have something to move
        _wheel(cdp, x, y, dy=-30, ctrl=True)
    out["scroll"] = _measure(cdp, scroll)

    main = _rect(cdp, "[data-bench=waveform] .cursor-crosshair")

    def hover() -> None:  # the time/bar readout follows the mouse
        end, k = time.monotonic() + seconds, 0
        while time.monotonic() < end:
            frac = (k % 240) / 240 if (k // 240) % 2 == 0 else 1 - (k % 240) / 240  # across and back, 2 s each
            cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=main[0] + main[2] * frac, y=main[1] + main[3] / 2)
            k += 1
            time.sleep(1 / 120)

    out["hover"] = _measure(cdp, hover)
    cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=1, y=1)
    time.sleep(2.6)  # the waveform only follows the playhead 2.5 s after the last manual scroll
    cdp.eval("window.__seedboxBench.follow('page')")
    status = cdp.eval("window.__seedboxBench.play(0)")
    if status != "playing":
        out["play"] = out["play_centred"] = {"error": f"playback didn't start (player status {status!r})"}
    else:
        out["play"] = _measure(cdp, lambda: time.sleep(seconds))
        # the waveform scrolls under a fixed playhead: everything repaints, and the playhead has to run evenly
        cdp.eval("window.__seedboxBench.follow('centred')")
        cdp.eval("window.__seedboxBench.traceStart()")
        out["play_centred"] = _measure(cdp, lambda: time.sleep(seconds))
        out["play_centred"].update({k: round(v, 3) for k, v in cdp.eval("window.__seedboxBench.traceStop()").items() if k != "frames"})
        cdp.eval("window.__seedboxBench.stop()")

    cdp.eval("document.querySelector('[data-bench=table]').scrollIntoView({block: 'center'})")
    time.sleep(0.3)
    table = _rect(cdp, "[data-bench=table] > div")
    tx, ty = table[0] + table[2] / 2, table[1] + table[3] / 2

    def list_scroll() -> None:  # 10,000 rows, down and back up
        end, k = time.monotonic() + seconds, 0
        while time.monotonic() < end:
            _wheel(cdp, tx, ty, dy=100 if (k // 120) % 2 == 0 else -100)
            k += 1
            time.sleep(1 / 120)

    out["list"] = _measure(cdp, list_scroll)
    return out


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(10)


def _drive(cdp: Cdp, url: str, seconds: float, cpu_throttle: float, size: tuple[int, int]) -> dict:
    agent = cdp.eval("navigator.userAgent")
    if cpu_throttle > 1:
        cdp.call("Emulation.setCPUThrottlingRate", rate=cpu_throttle)
    cdp.call("Page.enable")  # addScriptToEvaluateOnNewDocument needs it
    cdp.call("Page.addScriptToEvaluateOnNewDocument", source=COMMITS_JS)
    cdp.call("Page.navigate", url=f"{url.rstrip('/')}/#/bench")
    _wait(lambda: cdp.eval("!!window.__seedboxBench?.ready && !!document.querySelector('[data-bench=waveform] canvas')"),
          30, "the #/bench page")  # fmt: skip
    cdp.eval(METER_JS)
    time.sleep(0.5)  # let the first frames and layout settle
    gpu = cdp.eval("window.__seedboxBench.gpu?.() ?? null")  # the waveform renderer, and the GPU behind it
    return {"browser": agent, "cpu_throttle": cpu_throttle, "viewport": list(size), "waveform_renderer": gpu,
            "scenarios": {**scenarios(cdp, seconds), **now_playing(cdp, url, seconds)}}


def run(dist: Path = DIST, seconds: float = 5.0, cpu_throttle: float = 1.0, size: tuple[int, int] = (1440, 900),
        chrome: str | None = None, software_gl: bool = False, log=print) -> dict:
    """{"browser", "cpu_throttle", "scenarios": {name: {fps, frame_p95_ms, long_tasks, ...}}}, or {"skipped": why}."""
    chrome = chrome or find_chrome()
    if chrome is None:
        return {"skipped": "no Chrome, Chromium or Edge found (set $CHROME)"}
    if not (dist / "index.html").exists():
        return {"skipped": f"{dist} isn't built (cd frontend2 && deno task build)"}
    from websockets.sync.client import connect

    from seedbox2.server import start_server

    server = start_server(dist, port=0, power_mode="off")  # the 60 fps budget is for the normal mode, on battery too
    try:
        with tempfile.TemporaryDirectory(prefix="seedbox2-bench-chrome-") as profile:
            proc, errors, ws = _launch(chrome, Path(profile), size, software_gl)
            try:
                with connect(ws, max_size=None, open_timeout=30) as conn:
                    result = _drive(Cdp(conn), server.url, seconds, cpu_throttle, size)
            finally:
                _stop(proc)
                errors.close()
    finally:
        server.stop()
    log(f"frontend: {chrome}, {seconds:g} s per scenario, CPU throttle {cpu_throttle:g}x, "
        f"waveforms on {result.get('waveform_renderer') or 'unknown'}")
    for name, sc in result["scenarios"].items():
        if "error" in sc:
            log(f"frontend  {name}: ERROR {sc['error']}")
        else:
            log(f"frontend  {name}: {sc['fps']:.1f} fps, p95 frame {sc['frame_p95_ms']:.1f} ms, "
                f"{sc['long_tasks']} long tasks (longest {sc['long_task_ms_max']:.0f} ms), "
                f"{sc['react_commits_per_s']:.1f} React commits/s")
    return result


ENGINE_CPU_BUDGET = 15.0  # % of one core, 2 decks x 4 stems on the reference laptops (seedbox2/README.md)


def engine_ok(result: dict) -> bool:
    """Within budget, and the worklet computed the same samples as the core outside it. A skip isn't a failure."""
    if "skipped" in result:
        return True
    return all(r["cpu_percent"] < ENGINE_CPU_BUDGET and r["worklet_vs_core_max"] < 1e-5 for r in result["scenarios"].values())


def run_engine(dist: Path = DIST, seconds: float = 30.0, chrome: str | None = None, log=print) -> dict:
    """The real AudioWorklet in an OfflineAudioContext, flat out: wall time over audio time is the share of one core it
    takes. `on_tempo` copies its stems; `off_tempo` also resamples each (varispeed), the most it does."""
    chrome = chrome or find_chrome()
    if chrome is None:
        return {"skipped": "no Chrome, Chromium or Edge found (set $CHROME)"}
    if not (dist / "index.html").exists():
        return {"skipped": f"{dist} isn't built (cd frontend2 && deno task build)"}
    from websockets.sync.client import connect

    from seedbox2.server import start_server

    server = start_server(dist, port=0)
    try:
        with tempfile.TemporaryDirectory(prefix="seedbox2-bench-chrome-") as profile:
            proc, errors, ws = _launch(chrome, Path(profile), (800, 600))
            try:
                with connect(ws, max_size=None, open_timeout=30) as conn:
                    cdp = Cdp(conn)
                    agent = cdp.eval("navigator.userAgent")
                    cdp.call("Page.enable")
                    cdp.call("Page.navigate", url=f"{server.url.rstrip('/')}/#/bench")
                    _wait(lambda: cdp.eval("!!window.__seedboxBench?.ready"), 30, "the #/bench page")
                    scenarios_ = {}
                    for name, off in (("on_tempo", "false"), ("off_tempo", "true")):
                        scenarios_[name] = cdp.eval(f"window.__seedboxBench.engine({seconds}, {off})", timeout=seconds * 20 + 60)
            finally:
                _stop(proc)
                errors.close()
    finally:
        server.stop()
    for name, r in scenarios_.items():
        log(f"engine  {name}: {r['cpu_percent']:.1f}% of one core ({r['seconds']:g} s of audio in {r['wall_s']:.2f} s), "
            f"worklet vs core {r['worklet_vs_core_max']:.1e}")
    return {"browser": agent, "budget_percent": ENGINE_CPU_BUDGET, "scenarios": scenarios_}
