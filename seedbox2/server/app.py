"""create_app(): the Seedbox 2 FastAPI app, its routers, error handlers and the built frontend (frontend2/dist)."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, JSONResponse, Response

from seedbox2 import __version__
from seedbox2.server.events import HEARTBEAT_S, EventBus
from seedbox2.server.routers import ROUTERS

log = logging.getLogger(__name__)

DEV_ORIGINS = ["http://localhost:5174", "http://127.0.0.1:5174"]  # frontend2's Vite (Seedbox 1's is on 5173)


def create_app(static_dir: Path | None = None, *, dev: bool = False, heartbeat_s: float = HEARTBEAT_S) -> FastAPI:
    """The Seedbox 2 API under /api, and the built frontend at / when `static_dir` (a Vite dist/) is given.

    Routers reach shared state through `request.app.state`: `events` (the EventBus) and `static_dir`.
    """
    app = FastAPI(title="Seedbox 2", version=__version__, docs_url="/api/docs" if dev else None, redoc_url=None,
                  openapi_url="/api/openapi.json" if dev else None)  # fmt: skip
    app.state.events = EventBus(heartbeat_s=heartbeat_s)
    app.state.static_dir = Path(static_dir) if static_dir is not None else None
    if dev:
        app.add_middleware(CORSMiddleware, allow_origins=DEV_ORIGINS, allow_methods=["*"], allow_headers=["*"])
    _add_error_handlers(app)
    for router in ROUTERS:
        app.include_router(router, prefix="/api")
    if static_dir is not None:
        _add_spa(app, Path(static_dir))
    return app


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def _add_spa(app: FastAPI, static_dir: Path) -> None:
    root = static_dir.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> Response:
        if full_path == "api" or full_path.startswith("api/"):
            raise StarletteHTTPException(404, "Not Found")
        if full_path:
            candidate = root / full_path
            if _inside(candidate, root) and candidate.is_file():
                hashed = full_path.startswith("assets/")  # Vite's content-hashed bundles
                return FileResponse(candidate, headers={"Cache-Control": "public, max-age=31536000, immutable"}
                                    if hashed else None)  # fmt: skip
        index = root / "index.html"
        if not index.is_file():
            raise StarletteHTTPException(404, "frontend not built (cd frontend2 && deno task build)")
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


def _add_error_handlers(app: FastAPI) -> None:
    """Every error is `{detail: str}` JSON, as in Seedbox 1."""

    @app.exception_handler(RequestValidationError)
    async def validation_error(_req: Request, e: RequestValidationError) -> Response:
        parts = []
        for err in e.errors():
            where = ".".join(str(x) for x in err.get("loc", ()) if x not in ("body", "query", "path"))
            parts.append(f"{where}: {err.get('msg')}" if where else str(err.get("msg")))
        return JSONResponse({"detail": "; ".join(parts) or "invalid request"}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_req: Request, e: StarletteHTTPException) -> Response:
        return JSONResponse({"detail": e.detail}, status_code=e.status_code, headers=getattr(e, "headers", None))

    @app.exception_handler(Exception)
    async def unexpected(_req: Request, e: Exception) -> Response:
        log.exception("unhandled error")
        return JSONResponse({"detail": f"internal error: {type(e).__name__}: {e}"}, status_code=500)
