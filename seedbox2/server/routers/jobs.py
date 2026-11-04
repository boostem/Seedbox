"""/jobs: the persisted, prioritised, cancellable job queue (#39). Progress comes on /api/events (`job.*`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from seedbox2.jobs import PRIORITY, UnknownJobType
from seedbox2.library.repo import JOB_STATES
from seedbox2.server.deps import JobsDep

router = APIRouter(prefix="/jobs", tags=["jobs"])


class Submit(BaseModel):
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int | str = "library"  # a number, or what it is for: set, crate, library, background
    front: bool = False  # ahead of everything queued


class Bump(BaseModel):
    ids: list[str] = Field(default_factory=list)
    hashes: list[str] = Field(default_factory=list)  # the jobs about these tracks (e.g. the set just opened)


@router.get("")
def jobs(sup: JobsDep, state: str | None = None) -> dict:
    if state is not None and state not in JOB_STATES:
        raise HTTPException(422, f"state must be one of {', '.join(JOB_STATES)}")
    return {"status": sup.status(), "types": sup.registry.names(),
            "pools": {n: sup.registry.get(n).pool for n in sup.registry.names()}, "items": sup.jobs(state)}  # fmt: skip


@router.post("", status_code=202)
def submit(body: Submit, sup: JobsDep) -> dict:
    if isinstance(body.priority, str) and body.priority not in PRIORITY:
        raise HTTPException(422, f"priority must be a number or one of {', '.join(PRIORITY)}")
    try:
        return sup.submit(body.kind, body.payload, priority=body.priority, front=body.front)
    except UnknownJobType:
        raise HTTPException(404, f"no job type {body.kind!r}") from None


@router.post("/bump")
def bump(body: Bump, sup: JobsDep) -> dict:
    return {"ids": sup.bump(body.ids, body.hashes)}


@router.get("/{job_id}")
def job(job_id: str, sup: JobsDep) -> dict:
    row = sup.job(job_id)
    if row is None:
        raise HTTPException(404, "no such job")
    return row


@router.post("/{job_id}/cancel")
def cancel(job_id: str, sup: JobsDep) -> dict:
    row = sup.cancel(job_id)
    if row is None:
        raise HTTPException(404, "no such job")
    return row
