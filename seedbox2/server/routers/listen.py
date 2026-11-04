"""/listen: the blind A/B listening test (#86). The logic is in seedbox2/eval/listening.py.

Nothing a client gets before it has answered says which render is which: audio goes by trial id and slot, with no
file name, and the page's summary is the only place the engines are named.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import FileResponse

from seedbox2.eval import listening
from seedbox2.server.deps import LibraryDep

router = APIRouter(prefix="/listen", tags=["listen"])

Score = Annotated[int, Field(ge=1, le=5)]


class Scores(BaseModel):
    smoothness: Score | None = None
    energy: Score | None = None
    dj: Score | None = None


class Rating(BaseModel):
    choice: Literal["a", "b"]
    scores: dict[Literal["a", "b"], Scores] = {}
    comment: str = Field("", max_length=listening.MAX_TEXT)
    rater: str = Field("", max_length=listening.MAX_NAME)


def _folder(lib: LibraryDep) -> Path:
    return Path(lib.db.path).parent / listening.FOLDER


def _trials(request: Request) -> listening.Trials:
    return request.app.state.listen_trials


@router.get("/status")
def status(lib: LibraryDep, rater: str = "") -> dict[str, Any]:
    folder = _folder(lib)
    versions = listening.scan(folder)
    ratings = lib.ratings(listening.TEST)
    return {"folder": str(folder), "pairs": len({v.pair for v in versions}), "renders": len(versions),
            "comparisons": len(listening.comparisons(versions)), "ratings": len(ratings),
            "progress": listening.progress(versions, ratings, rater)}  # fmt: skip


@router.get("/next")
def next_trial(request: Request, lib: LibraryDep, rater: str = "") -> dict[str, Any]:
    """A new trial: which pair to listen to, and the id its audio and its answer go by."""
    versions = listening.scan(_folder(lib))
    ratings = lib.ratings(listening.TEST)
    chosen = listening.pick(versions, ratings, rater)
    if chosen is None:
        raise HTTPException(404, "no pair has two renders yet")
    trial = _trials(request).add(*chosen)
    return {"id": trial.id, "pair": trial.a.pair, "progress": listening.progress(versions, ratings, rater)}


@router.get("/trials/{trial_id}/{slot}", response_class=FileResponse)
def audio(trial_id: str, slot: Literal["a", "b"], request: Request) -> FileResponse:
    trial = _trials(request).get(trial_id)
    if trial is None:
        raise HTTPException(404, "this trial has ended")
    version = trial.slot(slot)
    if not version.path.is_file():
        raise HTTPException(404, "this render is gone")
    # no filename, and not cached: the URL is all the page knows
    return FileResponse(version.path, media_type=version.media_type, headers={"Cache-Control": "no-store"})


@router.post("/trials/{trial_id}/rating", status_code=201)
def rate(trial_id: str, body: Rating, request: Request, lib: LibraryDep) -> dict[str, Any]:
    trial = _trials(request).take(trial_id)
    if trial is None:
        raise HTTPException(404, "this trial has ended")
    scores = {slot: s.model_dump(exclude_none=True) for slot, s in body.scores.items()}
    row = listening.make_rating(trial, body.choice, scores, body.comment, body.rater)
    lib.add_ratings(listening.TEST, [row])
    return {"ok": True, "ratings": len(lib.ratings(listening.TEST))}


@router.get("/summary")
def summary(lib: LibraryDep) -> dict[str, Any]:
    return listening.summary(lib.ratings(listening.TEST))


@router.get("/export")
def export(lib: LibraryDep) -> dict[str, Any]:
    return listening.export(lib.ratings(listening.TEST))


class Save(BaseModel):
    rater: str = Field("", max_length=listening.MAX_NAME)


@router.post("/export/save")
def save_export(body: Save, lib: LibraryDep) -> dict[str, Any]:
    """Writes the export to a file and says where: a browser download doesn't work in every app window."""
    ratings = lib.ratings(listening.TEST)
    return {"path": str(listening.save_export(_folder(lib), ratings, body.rater)), "ratings": len(ratings)}


@router.post("/import")
def import_ratings(lib: LibraryDep, doc: Annotated[Any, Body()]) -> dict[str, int]:
    try:
        rows, duplicates = listening.parse_import(doc, lib.ratings(listening.TEST))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    lib.add_ratings(listening.TEST, rows)
    return {"added": len(rows), "duplicates": duplicates, "ratings": len(lib.ratings(listening.TEST))}
