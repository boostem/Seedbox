"""ModelManager: what the server runs for the models. It reports their state, downloads them in background threads
(all missing ones on first run, unless `auto_download` is off), and deletes them.

Events on /api/events: `model.progress` `{name, done, total}` (coalesced per model), and the terminal `model.done`,
`model.failed` `{name, error}` and `model.cancelled`, each followed by a `models` event with the whole status.

Settings (the `models` object in settings.json): `accelerator` (CoreML on macOS, DirectML on Windows; default off)
`auto_download` (default on) and `low_power` (default off: analysis uses the small models, e.g. Beat This! small0
instead of final0).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from seedbox2.models import store
from seedbox2.models.registry import MODELS, ModelSpec, get

log = logging.getLogger(__name__)

SETTINGS_KEY = "models"
DEFAULT_SETTINGS = {"accelerator": False, "auto_download": True, "low_power": False}

Publish = Callable[..., None]  # publish(type, data, key=None, terminal=False), as EventBus.publish


def read_settings(settings: Any) -> dict:
    """The model settings from a seedbox2.library.service.Settings (or None: the defaults)."""
    raw = settings.get(SETTINGS_KEY, {}) if settings is not None else {}
    raw = raw if isinstance(raw, dict) else {}
    return {k: bool(raw.get(k, v)) for k, v in DEFAULT_SETTINGS.items()}


class _Download:
    def __init__(self) -> None:
        self.cancel = threading.Event()
        self.done = 0
        self.total = 0
        self.thread: threading.Thread | None = None


class ModelManager:
    def __init__(self, root: Path | None = None, *, settings: Any = None, publish: Publish | None = None,
                 models: dict[str, ModelSpec] | None = None, session: Any = None) -> None:  # fmt: skip
        self.root = store.models_dir() if root is None else Path(root)
        self.settings = settings
        self.models = MODELS if models is None else models
        self._publish = publish
        self._session = session  # a requests.Session (tests pass their own)
        self._lock = threading.Lock()
        self._downloads: dict[str, _Download] = {}
        self._errors: dict[str, str] = {}

    # --- state ---------------------------------------------------------------------------------------------------

    def model(self, name: str) -> dict:
        spec = get(name, self.models)
        installed = store.installed(spec, self.root)
        with self._lock:
            dl = self._downloads.get(name)
            error = self._errors.get(name)
        if dl is not None:
            state = "downloading"
        elif installed:
            state = "installed"
        elif error:
            state = "failed"
        else:
            state = "missing"
        return {
            **spec.to_json(),
            "state": state,
            "installed": installed,
            "path": str(store.model_path(spec, self.root)) if installed else None,
            "done": dl.done if dl else (spec.size if installed else store.partial_bytes(spec, self.root)),
            "error": error if state == "failed" else None,
        }

    def status(self) -> dict:
        items = [self.model(name) for name in self.models]
        missing = [m for m in items if not m["installed"]]
        return {
            "dir": str(self.root),
            "models": items,
            "missing": [{k: m[k] for k in ("name", "title", "used_for", "fallback")} for m in missing],
            "offline": bool(missing),  # something runs on the Seedbox 1 heuristics
            "installed_bytes": sum(m["size"] for m in items if m["installed"]),
            "total_bytes": sum(m["size"] for m in items),
            "settings": self.get_settings(),
        }

    def missing(self) -> list[str]:
        return [name for name, spec in self.models.items() if not store.installed(spec, self.root)]

    # --- settings ------------------------------------------------------------------------------------------------

    def get_settings(self) -> dict:
        return read_settings(self.settings)

    def update_settings(self, **changes: bool | None) -> dict:
        given = {k: bool(v) for k, v in changes.items() if v is not None and k in DEFAULT_SETTINGS}
        new = {**self.get_settings(), **given}
        if self.settings is not None:
            self.settings.set(SETTINGS_KEY, new)
        return new

    # --- downloads -----------------------------------------------------------------------------------------------

    def download(self, name: str, *, force: bool = False) -> dict:
        """Start downloading the model in the background (no-op if it is downloading or, without `force`, there).
        `force` deletes the installed file first (re-download)."""
        spec = get(name, self.models)
        with self._lock:
            if name not in self._downloads:
                if force:
                    store.delete(spec, self.root)
                if not store.installed(spec, self.root):
                    dl = self._downloads[name] = _Download()
                    dl.total = spec.size
                    self._errors.pop(name, None)
                    dl.thread = threading.Thread(target=self._run, args=(spec, dl), name=f"seedbox2-model-{name}",
                                                 daemon=True)  # fmt: skip
                    dl.thread.start()
        return self.model(name)

    def download_missing(self) -> list[str]:
        """Start the downloads of every model that isn't there (first run). Returns their names."""
        names = self.missing()
        for name in names:
            self.download(name)
        return names

    def start(self) -> None:
        """On app start: download what is missing when `auto_download` is on."""
        if self.get_settings()["auto_download"]:
            names = self.download_missing()
            if names:
                log.info("downloading models: %s", ", ".join(names))

    def cancel(self, name: str) -> bool:
        get(name, self.models)
        with self._lock:
            dl = self._downloads.get(name)
        if dl is None:
            return False
        dl.cancel.set()
        return True

    def delete(self, name: str) -> dict:
        """Delete the model's files (cancelling its download first)."""
        spec = get(name, self.models)
        with self._lock:
            dl = self._downloads.get(name)
        if dl is not None:
            dl.cancel.set()
            if dl.thread is not None:
                dl.thread.join(timeout=10)
        store.delete(spec, self.root)
        with self._lock:
            self._errors.pop(name, None)
        self._emit("models", self.status())
        return self.model(name)

    def wait(self, timeout: float | None = None) -> None:
        """Wait for the running downloads (tests, shutdown)."""
        with self._lock:
            threads = [d.thread for d in self._downloads.values() if d.thread is not None]
        for t in threads:
            t.join(timeout)

    def close(self) -> None:
        with self._lock:
            downloads = list(self._downloads.values())
        for dl in downloads:
            dl.cancel.set()
        self.wait(timeout=5)

    def _run(self, spec: ModelSpec, dl: _Download) -> None:
        def progress(done: int, total: int) -> None:
            dl.done, dl.total = done, total
            self._emit("model.progress", {"name": spec.name, "done": done, "total": total}, key=f"model:{spec.name}")

        outcome, data = "model.done", {"name": spec.name}
        try:
            store.download(spec, self.root, progress=progress, cancel=dl.cancel, session=self._session)
        except store.DownloadCancelled:
            outcome = "model.cancelled"
        except Exception as e:  # noqa: BLE001 - reported to the UI, the app goes on without the model
            log.warning("model %s: %s", spec.name, e)
            outcome, data = "model.failed", {"name": spec.name, "error": str(e)}
            with self._lock:
                self._errors[spec.name] = str(e)
        finally:
            with self._lock:
                self._downloads.pop(spec.name, None)
        self._emit(outcome, data, terminal=True)
        self._emit("models", self.status())

    def _emit(self, type_: str, data: Any, *, key: str | None = None, terminal: bool = False) -> None:
        if self._publish is None:
            return
        try:
            self._publish(type_, data, key=key, terminal=terminal)
        except Exception:  # noqa: BLE001 - the event bus must not break a download
            log.exception("couldn't publish %s", type_)
