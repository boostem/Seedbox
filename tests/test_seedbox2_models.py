"""Seedbox 2's model manager (#42) with a tiny dummy ONNX model served from a local HTTP server: the registry,
downloads that resume, checksums, versions, sessions (threads, providers, CPU fallback), offline mode and
/api/models."""

from __future__ import annotations

import hashlib
import http.server
import json
import re
import threading
import time
from dataclasses import replace

import numpy as np
import pytest

from seedbox2.models import manager as manager_mod
from seedbox2.models import runtime, store
from seedbox2.models.manager import ModelManager
from seedbox2.models.registry import MODELS, ModelSpec

onnx = pytest.importorskip("onnx")
ort = pytest.importorskip("onnxruntime")


def dummy_model_bytes() -> bytes:
    """y = 2x + 1 over a float32 [1, n] input: a valid ONNX graph of a few hundred bytes."""
    from onnx import TensorProto, helper

    x = helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, "n"])
    y = helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, "n"])
    two = helper.make_tensor("two", TensorProto.FLOAT, [], [2.0])
    one = helper.make_tensor("one", TensorProto.FLOAT, [], [1.0])
    nodes = [helper.make_node("Mul", ["x", "two"], ["t"]), helper.make_node("Add", ["t", "one"], ["y"])]
    graph = helper.make_graph(nodes, "dummy", [x], [y], initializer=[two, one])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], producer_name="seedbox2-tests")
    model.ir_version = 8  # readable by older onnxruntime builds too
    onnx.checker.check_model(model)
    # pad with a metadata string so the file spans several download chunks
    model.metadata_props.add(key="padding", value="x" * 300_000)
    return model.SerializeToString()


BODY = dummy_model_bytes()


class Server:
    """Serves BODY (or `body`) at /<anything>, with Range support, and records what it was asked."""

    def __init__(self) -> None:
        self.body = BODY
        self.ranges: list[str | None] = []
        self.honour_range = True
        self.cut_after: int | None = None  # close the connection after this many bytes, once
        self.status = 200
        self.slow_s = 0.0  # pause between 64 KB blocks
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_GET(self):  # noqa: N802 - the handler's API
                rng = self.headers.get("Range")
                outer.ranges.append(rng)
                if outer.status != 200:
                    self.send_error(outer.status)
                    return
                body, start = outer.body, 0
                m = re.fullmatch(r"bytes=(\d+)-", rng or "")
                if m and outer.honour_range:
                    start = int(m.group(1))
                    if start >= len(body):
                        self.send_error(416)
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
                else:
                    self.send_response(200)
                chunk = body[start:]
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                if outer.cut_after is not None:
                    n, outer.cut_after = outer.cut_after, None
                    self.wfile.write(chunk[:n])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                for i in range(0, len(chunk), 65536):
                    self.wfile.write(chunk[i:i + 65536])
                    if outer.slow_s:
                        time.sleep(outer.slow_s)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = Server()
    yield s
    s.close()


def spec_for(server: Server, *, name="dummy", version="1", body: bytes = BODY) -> ModelSpec:
    return ModelSpec(name=name, version=version, title="Dummy", file="dummy.onnx", url=f"{server.url}/dummy.onnx",
                     sha256=hashlib.sha256(body).hexdigest(), size=len(body), licence="MIT", attribution="the tests",
                     homepage="https://example.invalid", used_for="Testing", fallback="nothing")  # fmt: skip


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(store, "RETRY_WAIT_S", 0.01)


# --- the registry --------------------------------------------------------------------------------------------------


def test_every_registered_model_is_pinned_checksummed_and_credited():
    assert MODELS
    for name, spec in MODELS.items():
        assert spec.name == name
        assert re.fullmatch(r"[0-9a-f]{64}", spec.sha256), name
        assert spec.size > 0 and spec.version and spec.file.endswith((".onnx", ".ort"))
        assert spec.url.startswith("https://") and re.search(r"/[0-9a-f]{40}/", spec.url), "pin a revision"
        assert spec.licence and spec.attribution and spec.homepage and spec.fallback and spec.used_for


# --- downloads -----------------------------------------------------------------------------------------------------


def test_download_verifies_and_stores_in_the_cache_folder(server, tmp_path):
    spec = spec_for(server)
    seen = []
    path = store.download(spec, tmp_path, progress=lambda d, t: seen.append((d, t)))
    assert path == tmp_path / "dummy" / "1" / "dummy.onnx" and path.read_bytes() == BODY
    assert store.installed(spec, tmp_path) and store.verify(spec, tmp_path)
    assert not store.part_path(spec, tmp_path).exists()
    assert seen[-1] == (len(BODY), len(BODY)) and [d for d, _ in seen] == sorted(d for d, _ in seen)
    server.ranges.clear()
    assert store.download(spec, tmp_path) == path and server.ranges == []  # already there: no request


def test_models_dir_is_in_the_cache_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    assert store.models_dir() == tmp_path / "cache" / "models"


def test_download_resumes_a_partial_file(server, tmp_path):
    spec = spec_for(server)
    part = store.part_path(spec, tmp_path)
    part.parent.mkdir(parents=True)
    part.write_bytes(BODY[:100_000])
    store.download(spec, tmp_path)
    assert server.ranges == ["bytes=100000-"]
    assert store.verify(spec, tmp_path)


def test_a_dropped_connection_is_resumed_by_the_retry(server, tmp_path):
    spec = spec_for(server)
    server.cut_after = 123_456
    store.download(spec, tmp_path)
    assert server.ranges[0] is None and len(server.ranges) == 2
    resumed_at = int(re.fullmatch(r"bytes=(\d+)-", server.ranges[1]).group(1))
    assert 0 < resumed_at <= 123_456  # what arrived before the drop is kept, up to the block being read
    assert store.verify(spec, tmp_path)


def test_a_server_that_ignores_range_restarts_the_file(server, tmp_path):
    spec = spec_for(server)
    server.honour_range = False
    part = store.part_path(spec, tmp_path)
    part.parent.mkdir(parents=True)
    part.write_bytes(BODY[:5000])
    store.download(spec, tmp_path)
    assert store.verify(spec, tmp_path)


def test_a_wrong_checksum_deletes_the_download(server, tmp_path):
    spec = replace(spec_for(server), sha256="0" * 64)
    with pytest.raises(store.ChecksumMismatch):
        store.download(spec, tmp_path)
    assert not store.installed(spec, tmp_path) and not store.part_path(spec, tmp_path).exists()
    assert not store.model_path(spec, tmp_path).exists()


def test_http_errors_and_wrong_sizes_fail_after_the_retries(server, tmp_path):
    server.status = 404
    with pytest.raises(store.DownloadError, match="HTTP 404"):
        store.download(spec_for(server), tmp_path, retries=2)
    assert len(server.ranges) == 2
    server.status = 200
    small = replace(spec_for(server), size=1000)
    with pytest.raises(store.DownloadError, match="larger"):
        store.download(small, tmp_path, retries=1)


def test_cancel_keeps_the_partial_file_for_later(server, tmp_path):
    spec = spec_for(server)
    cancel = threading.Event()

    def progress(done, total):
        if done > 0:
            cancel.set()

    server.slow_s = 0.01
    with pytest.raises(store.DownloadCancelled):
        store.download(spec, tmp_path, progress=progress, cancel=cancel)
    assert 0 < store.partial_bytes(spec, tmp_path) < len(BODY)
    server.slow_s = 0
    store.download(spec, tmp_path)
    assert store.verify(spec, tmp_path)


def test_a_new_version_replaces_the_old_one_and_delete_removes_all(server, tmp_path):
    old = spec_for(server, version="1")
    store.download(old, tmp_path)
    new_body = BODY + b"\0"  # trailing bytes: a different file
    server.body = new_body
    new = spec_for(server, version="2", body=new_body)
    store.download(new, tmp_path)
    assert [p.name for p in (tmp_path / "dummy").iterdir()] == ["2"]
    assert store.delete(new, tmp_path) and not (tmp_path / "dummy").exists()
    assert not store.delete(new, tmp_path)


# --- sessions ------------------------------------------------------------------------------------------------------


def test_threads_are_the_pools_share_of_the_physical_cores():
    assert runtime.intra_op_threads("light", 6, cores=4) == 1  # the 4-core reference laptop: 6 light workers
    assert runtime.intra_op_threads("light", 2, cores=8) == 4
    assert runtime.intra_op_threads("heavy", 1, cores=4) == 2
    assert runtime.intra_op_threads("heavy", 1, cores=1) == 1
    assert runtime.physical_cores() >= 1


def test_providers_use_the_accelerator_only_when_asked_and_always_keep_the_cpu():
    avail = ["CoreMLExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]
    assert runtime.providers(True, "darwin", avail) == ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    assert runtime.providers(True, "win32", avail) == ["DmlExecutionProvider", "CPUExecutionProvider"]
    assert runtime.providers(False, "darwin", avail) == ["CPUExecutionProvider"]
    assert runtime.providers(True, "linux", avail) == ["CPUExecutionProvider"]
    assert runtime.providers(True, "win32", ["CPUExecutionProvider"]) == ["CPUExecutionProvider"]  # plain onnxruntime
    assert runtime.providers()[-1] == "CPUExecutionProvider"


def test_session_options():
    opts = runtime.session_options(3)
    assert opts.intra_op_num_threads == 3 and opts.inter_op_num_threads == 1


def test_load_session_runs_the_model_and_raises_model_missing_without_it(server, tmp_path):
    spec = spec_for(server)
    models = {"dummy": spec}
    with pytest.raises(runtime.ModelMissing) as e:
        runtime.load_session("dummy", root=tmp_path, models=models)
    assert "nothing" in str(e.value)  # says what is used instead
    store.download(spec, tmp_path)
    sess = runtime.load_session("dummy", root=tmp_path, models=models, pool="light", pool_size=1)
    (y,) = sess.run(None, {"x": np.array([[1.0, 2.0, 3.0]], np.float32)})
    np.testing.assert_allclose(y, [[3.0, 5.0, 7.0]])
    assert sess.get_providers()[-1] == "CPUExecutionProvider"


def test_an_accelerator_that_fails_falls_back_to_the_cpu(server, tmp_path, monkeypatch):
    spec = spec_for(server)
    store.download(spec, tmp_path)
    real = ort.InferenceSession
    asked = []

    def fake(path, sess_options=None, providers=None):
        asked.append(providers)
        if providers != ["CPUExecutionProvider"]:
            raise RuntimeError("no such device")
        return real(path, sess_options=sess_options, providers=providers)

    monkeypatch.setattr(ort, "InferenceSession", fake)
    monkeypatch.setattr(runtime, "providers", lambda *a, **k: ["CoreMLExecutionProvider", "CPUExecutionProvider"])
    sess = runtime.open_session(store.model_path(spec, tmp_path), accelerator=True)
    assert asked == [["CoreMLExecutionProvider", "CPUExecutionProvider"], ["CPUExecutionProvider"]]
    assert sess.run(None, {"x": np.zeros((1, 2), np.float32)})[0].tolist() == [[1.0, 1.0]]


class _Conn:
    def send(self, msg):
        pass


def test_a_job_context_keeps_one_session_per_worker_and_falls_back_when_missing(server, tmp_path, monkeypatch):
    from seedbox2.jobs.worker import JobContext

    spec = spec_for(server)
    monkeypatch.setattr("seedbox2.models.runtime.MODELS", {"dummy": spec})
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    made = []
    real = runtime.open_session
    monkeypatch.setattr(runtime, "open_session", lambda *a, **k: made.append(k) or real(*a, **k))
    cache: dict = {}
    ctx = JobContext("j", _Conn(), threading.Event(), cache, tmp_path / "library.sqlite3", "heavy", 1)
    assert ctx.session_or_none("dummy") is None  # offline: the job uses the Seedbox 1 heuristics
    store.download(spec, store.models_dir())
    (tmp_path / "settings.json").write_text(json.dumps({"models": {"accelerator": True}}))
    first = ctx.session("dummy")
    again = JobContext("k", _Conn(), threading.Event(), cache, tmp_path / "library.sqlite3", "heavy", 1)
    assert again.session("dummy") is first and len(made) == 1
    assert made[0]["threads"] == runtime.intra_op_threads("heavy", 1) and made[0]["accelerator"] is True


# --- the manager and offline mode ----------------------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, object, str | None, bool]] = []

    def publish(self, type_, data=None, *, key=None, terminal=False):
        self.events.append((type_, data, key, terminal))


class FakeSettings:
    def __init__(self) -> None:
        self.data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


def test_manager_downloads_what_is_missing_on_first_run_and_reports_it(server, tmp_path):
    rec = Recorder()
    a, b = spec_for(server, name="a"), spec_for(server, name="b")
    mgr = ModelManager(tmp_path, settings=FakeSettings(), publish=rec.publish, models={"a": a, "b": b})
    status = mgr.status()
    assert status["offline"] and [m["name"] for m in status["missing"]] == ["a", "b"]
    assert status["missing"][0]["fallback"] == "nothing" and {m["state"] for m in status["models"]} == {"missing"}
    mgr.start()
    mgr.wait(10)
    status = mgr.status()
    assert not status["offline"] and status["missing"] == []
    assert {m["state"] for m in status["models"]} == {"installed"} and status["installed_bytes"] == 2 * len(BODY)
    types = [t for t, *_ in rec.events]
    assert types.count("model.done") == 2 and "model.progress" in types and types[-1] == "models"
    progress = [e for e in rec.events if e[0] == "model.progress"]
    assert all(key == f"model:{data['name']}" for _, data, key, _ in progress)  # coalesced per model
    assert all(terminal for t, _, _, terminal in rec.events if t == "model.done")


def test_manager_does_not_download_when_auto_download_is_off(server, tmp_path):
    settings = FakeSettings()
    mgr = ModelManager(tmp_path, settings=settings, models={"a": spec_for(server, name="a")})
    assert mgr.update_settings(auto_download=False, accelerator=True) == {"accelerator": True, "auto_download": False}
    assert settings.data == {"models": {"accelerator": True, "auto_download": False}}
    mgr.start()
    mgr.wait(5)
    assert server.ranges == [] and mgr.status()["offline"]


def test_a_failed_download_says_why_and_the_app_goes_on(server, tmp_path):
    server.status = 503
    rec = Recorder()
    mgr = ModelManager(tmp_path, publish=rec.publish, models={"a": spec_for(server, name="a")})
    mgr.download("a")
    mgr.wait(10)
    model = mgr.model("a")
    assert model["state"] == "failed" and "503" in model["error"]
    assert ("model.failed", {"name": "a", "error": model["error"]}, None, True) in rec.events
    server.status = 200
    mgr.download("a")
    mgr.wait(10)
    assert mgr.model("a")["state"] == "installed" and mgr.model("a")["error"] is None


def test_redownload_and_delete(server, tmp_path):
    mgr = ModelManager(tmp_path, models={"a": spec_for(server, name="a")})
    mgr.download("a")
    mgr.wait(10)
    server.ranges.clear()
    mgr.download("a")  # installed: nothing to do
    mgr.wait(10)
    assert server.ranges == []
    mgr.download("a", force=True)
    mgr.wait(10)
    assert server.ranges == [None] and mgr.model("a")["installed"]
    assert mgr.delete("a")["state"] == "missing" and not (tmp_path / "a").exists()


def test_cancel_a_download(server, tmp_path):
    server.slow_s = 0.05
    rec = Recorder()
    mgr = ModelManager(tmp_path, publish=rec.publish, models={"a": spec_for(server, name="a")})
    assert mgr.download("a")["state"] == "downloading"
    deadline = time.monotonic() + 5
    while not any(t == "model.progress" and d["done"] for t, d, *_ in rec.events) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert mgr.cancel("a")
    mgr.wait(10)
    assert mgr.model("a")["state"] == "missing" and ("model.cancelled", {"name": "a"}, None, True) in rec.events
    assert not mgr.cancel("a")


# --- /api/models ---------------------------------------------------------------------------------------------------


@pytest.fixture
def client(server, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from seedbox2.server.app import create_app

    monkeypatch.setattr(manager_mod, "MODELS", {"dummy": spec_for(server)})
    app = create_app(library_path=tmp_path / "library.sqlite3", models_dir=tmp_path / "models")
    with TestClient(app) as c:
        yield c


def wait_state(client, want, timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        model = client.get("/api/models").json()["models"][0]
        if model["state"] == want or time.monotonic() > deadline:
            return model
        time.sleep(0.02)


def test_models_api(client, tmp_path):
    status = client.get("/api/models").json()
    assert status["offline"] and status["dir"] == str(tmp_path / "models")
    model = status["models"][0]
    assert model["name"] == "dummy" and model["licence"] == "MIT" and model["size"] == len(BODY)
    assert client.post("/api/models/dummy/download").status_code == 202
    assert wait_state(client, "installed")["path"].endswith("dummy.onnx")
    again = client.post("/api/models/dummy/download", json={"force": True}).json()
    assert again["state"] in ("downloading", "installed")
    wait_state(client, "installed")
    assert client.delete("/api/models/dummy").json()["state"] == "missing"
    assert client.post("/api/models/download").json()["models"][0]["state"] in ("downloading", "installed")
    wait_state(client, "installed")
    settings = client.post("/api/models/settings", json={"accelerator": True}).json()["settings"]
    assert settings == {"accelerator": True, "auto_download": True}
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["models"] == settings
    assert client.post("/api/models/nope/download").status_code == 404
    assert client.delete("/api/models/nope").status_code == 404
    assert client.post("/api/models/dummy/cancel").json() == {"cancelled": False}


def test_the_app_starts_and_works_offline(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from seedbox2.server.app import create_app

    unreachable = ModelSpec(name="dummy", version="1", title="Dummy", file="dummy.onnx",
                            url="http://127.0.0.1:9/dummy.onnx", sha256="0" * 64, size=10, licence="MIT",
                            attribution="x", homepage="x", used_for="Testing", fallback="the heuristics")  # fmt: skip
    monkeypatch.setattr(manager_mod, "MODELS", {"dummy": unreachable})
    monkeypatch.setattr(store, "RETRIES", 1)
    app = create_app(library_path=tmp_path / "library.sqlite3", models_dir=tmp_path / "models", download_models=True)
    with TestClient(app) as c:
        assert c.get("/api/health").json() == {"ok": True}
        model = wait_state(c, "failed")
        assert model["error"]
        status = c.get("/api/models").json()
        assert status["offline"] and status["missing"][0]["fallback"] == "the heuristics"


# --- the export scripts' parity helpers (dev-only: torch) -------------------------------------------------------------


def test_parity_compares_onnx_with_torch(tmp_path):
    torch = pytest.importorskip("torch")
    from seedbox2.models.export import common

    class Twice(torch.nn.Module):
        def forward(self, x):
            return x * 2 + 1

    path = tmp_path / "twice.onnx"
    common.export(Twice().eval(), (torch.zeros(1, 4),), path, ["x"], ["y"], {"x": {1: "n"}})
    report = common.parity(Twice().eval(), path, [{"x": np.ones((1, n), np.float32)} for n in (3, 9)])
    assert report.ok and report.max_error < 1e-6 and len(report.cases) == 2
    entry = common.registry_entry(path)
    assert entry["size"] == path.stat().st_size and entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


# --- the real models (nightly: `pytest -m models`, downloads kept in $SEEDBOX_CACHE_DIR) -----------------------------


@pytest.mark.models
@pytest.mark.parametrize("name", sorted(MODELS))
def test_registered_model_downloads_verifies_and_runs(name):
    spec = MODELS[name]
    root = store.models_dir()
    store.download(spec, root)
    assert store.verify(spec, root)
    sess = runtime.load_session(name, root=root)
    feed = {}
    for i in sess.get_inputs():
        shape = [d if isinstance(d, int) else 64 for d in i.shape]
        feed[i.name] = np.zeros(shape, np.float32)
    outputs = sess.run(None, feed)
    assert outputs and all(np.isfinite(o).all() for o in outputs)
