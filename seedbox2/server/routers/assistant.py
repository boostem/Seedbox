"""/assistant: the optional set assistant (#63). Off by default; it needs an API key and it costs money per run. Nothing
here is needed by anything else in the app.

- `GET /assistant`: `{enabled, model, models, has_key, keychain}`. The key itself never comes back from anywhere.
- `POST /assistant/settings` with `enabled` and/or `model`.
- `PUT /assistant/key` with `key` stores it in the OS keychain; `DELETE` on it removes it.
- `POST /assistant/run` with `{prompt, set_id, crate_ids}`: a draft set made from the tracks of that set and those crates,
  or from the whole library with neither. Saves nothing: `POST /sets` and `PUT /sets/{id}/order` keep it. 409 if one is
  already running, or if the assistant is off or has no key.
- `POST /assistant/cancel`: stops a run before its next request.
- `POST /assistant/dry-run`, same body: what a run would send first. No key and no network.

Events: `assistant.step` (`{text}`) as a run goes.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from seedbox2.analysis import vibe
from seedbox2.assistant import AssistantError, Config, KeyStore, Session, agent, make_client
from seedbox2.library import Library
from seedbox2.library.service import SETTINGS_FILE, Settings
from seedbox2.server.deps import LibraryDep, get_jobs

router = APIRouter(prefix="/assistant", tags=["assistant"])

_running = threading.Lock()
_stop = threading.Event()


def get_config(lib: LibraryDep) -> Config:
    return Config(Settings(Path(lib.db.path).parent / SETTINGS_FILE), KeyStore())


ConfigDep = Annotated[Config, Depends(get_config)]


class SettingsBody(BaseModel):
    enabled: bool | None = None
    model: str | None = None


class KeyBody(BaseModel):
    key: str


class RunBody(BaseModel):
    prompt: str = ""
    set_id: int | None = None
    crate_ids: list[int] = []


def _http(e: AssistantError) -> HTTPException:
    return HTTPException(e.status, str(e))


@router.get("")
def status(config: ConfigDep) -> dict:
    return config.status()


@router.post("/settings")
def settings(body: SettingsBody, config: ConfigDep) -> dict:
    try:
        return config.update(enabled=body.enabled, model=body.model)
    except AssistantError as e:
        raise _http(e) from None


@router.put("/key")
def put_key(body: KeyBody, config: ConfigDep) -> dict:
    try:
        return config.set_key(body.key)
    except AssistantError as e:
        raise _http(e) from None


@router.delete("/key")
def delete_key(config: ConfigDep) -> dict:
    try:
        return config.delete_key()
    except AssistantError as e:
        raise _http(e) from None


def _scope(lib: Library, body: RunBody) -> list[str] | None:
    """The hashes allowed, or None for the whole library."""
    if body.set_id is None and not body.crate_ids:
        return None
    hashes: list[str] = []
    if body.set_id is not None:
        in_set = lib.set_hashes(body.set_id)
        if in_set is None:
            raise HTTPException(404, "no such set")
        hashes += in_set
    for crate_id in body.crate_ids:
        in_crate = lib.crate_hashes(crate_id)
        if in_crate is None:
            raise HTTPException(404, "no such crate")
        hashes += in_crate
    return list(dict.fromkeys(hashes))


def _vibe_search(request: Request, lib: Library) -> Any:
    """Each track's similarity to a text, from the same index as `/analysis/vibe/search`."""
    from seedbox2.server.routers.analysis import _index

    def search(text: str) -> dict[str, float]:
        query = vibe.text_vector(get_jobs(request), text)
        index = _index(request)
        index.refresh(lib)
        return dict(zip(index.hashes, (float(s) for s in index.best_per_track(query)), strict=True))

    return search


@router.post("/dry-run")
def dry_run(body: RunBody, lib: LibraryDep, config: ConfigDep) -> dict:
    try:
        return agent.dry_run(config.model, body.prompt, Session(lib, _scope(lib, body)))
    except AssistantError as e:
        raise _http(e) from None


@router.post("/run")
def run(body: RunBody, request: Request, lib: LibraryDep, config: ConfigDep) -> dict:
    if not _running.acquire(blocking=False):
        raise HTTPException(409, "The assistant is already working on a set")
    try:
        _stop.clear()
        make = getattr(request.app.state, "assistant_client", None) or make_client  # tests pass a fake
        client = make(config.require_key())
        session = Session(lib, _scope(lib, body), vibe=_vibe_search(request, lib))
        events = request.app.state.events
        return agent.run(client, config.model, body.prompt, session, cancelled=_stop.is_set,
                         on_step=lambda text: events.publish("assistant.step", {"text": text}))  # fmt: skip
    except AssistantError as e:
        raise _http(e) from None
    finally:
        _running.release()


@router.post("/cancel")
def cancel() -> dict:
    _stop.set()
    return {"stopping": _running.locked()}
