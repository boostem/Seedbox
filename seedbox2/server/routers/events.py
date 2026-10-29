"""/events: the one server-sent event stream. Every area publishes to request.app.state.events (an EventBus)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response, StreamingResponse

router = APIRouter(tags=["events"])


@router.get("/events")
async def events(request: Request) -> Response:
    async def disconnected() -> None:
        while (await request.receive())["type"] != "http.disconnect":
            pass

    return StreamingResponse(
        request.app.state.events.stream(disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
