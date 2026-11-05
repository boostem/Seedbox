"""/power: low-power mode (#85): whether it's on, why and what it holds back; its setting. Changes come on
/api/events as `power`, with the same status."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from seedbox2.server.deps import PowerDep

router = APIRouter(prefix="/power", tags=["power"])


class PowerSettings(BaseModel):
    mode: Literal["auto", "on", "off"]


@router.get("")
def power(monitor: PowerDep) -> dict:
    return monitor.status()


@router.post("/settings")
def settings(body: PowerSettings, monitor: PowerDep) -> dict:
    return monitor.set_mode(body.mode)
