"""/models: the downloadable models (#42), their licences and state; download, re-download, cancel, delete and the
model settings. Download progress comes on /api/events (`model.progress`, `model.done`, `model.failed`, `models`)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from seedbox2.models.registry import UnknownModel
from seedbox2.server.deps import ModelsDep

router = APIRouter(prefix="/models", tags=["models"])


class Download(BaseModel):
    force: bool = False  # delete the installed file and download it again


class SettingsBody(BaseModel):
    accelerator: bool | None = None  # CoreML on macOS, DirectML on Windows (the CPU stays the fallback)
    auto_download: bool | None = None  # download missing models when the app starts
    low_power: bool | None = None  # analysis uses the small models (Beat This! small0)


@router.get("")
def models(mgr: ModelsDep) -> dict:
    return mgr.status()


@router.post("/settings")
def settings(body: SettingsBody, mgr: ModelsDep) -> dict:
    mgr.update_settings(accelerator=body.accelerator, auto_download=body.auto_download, low_power=body.low_power)
    return mgr.status()


@router.post("/download")
def download_missing(mgr: ModelsDep) -> dict:
    mgr.download_missing()
    return mgr.status()


@router.post("/{name}/download", status_code=202)
def download(name: str, mgr: ModelsDep, body: Download | None = None) -> dict:
    try:
        return mgr.download(name, force=bool(body and body.force))
    except UnknownModel:
        raise HTTPException(404, f"no model {name!r}") from None


@router.post("/{name}/cancel")
def cancel(name: str, mgr: ModelsDep) -> dict:
    try:
        return {"cancelled": mgr.cancel(name)}
    except UnknownModel:
        raise HTTPException(404, f"no model {name!r}") from None


@router.delete("/{name}")
def delete(name: str, mgr: ModelsDep) -> dict:
    try:
        return mgr.delete(name)
    except UnknownModel:
        raise HTTPException(404, f"no model {name!r}") from None
