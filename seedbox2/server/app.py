"""create_app(): the Seedbox 2 FastAPI app, its routers, error handlers and the built frontend (frontend2/dist)."""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response

from seedbox2 import __version__
from seedbox2.eval.listening import Trials
from seedbox2.server.events import HEARTBEAT_S, EventBus
from seedbox2.server.routers import ROUTERS

log = logging.getLogger(__name__)

DEV_ORIGINS = ["http://localhost:5174", "http://127.0.0.1:5174"]  # frontend2's Vite (Seedbox 1's is on 5173)
NOT_BUILT_HTML = """<!doctype html><title>Seedbox 2</title>
<body style="background:#0b0d10;color:#e8e8e8;font:14px system-ui;padding:48px">
<h2>The Seedbox 2 interface isn't built yet</h2>
<p>Seedbox 2 tried to build it and couldn't (the terminal says why). It needs <a style="color:inherit" href="https://deno.com">deno</a>. In the project folder, run:</p>
<pre style="background:#16191e;padding:12px;border-radius:6px">cd frontend2 &amp;&amp; deno install &amp;&amp; deno task build</pre>
<p>Then start Seedbox 2 again.</p>
</body>
"""


def create_app(static_dir: Path | None = None, *, dev: bool = False, heartbeat_s: float = HEARTBEAT_S,
               library_path: Path | None = None, start_jobs: bool = False, watch_folders: bool = False,
               download_models: bool = False, models_dir: Path | None = None) -> FastAPI:
    """The Seedbox 2 API under /api, and the built frontend at / when `static_dir` (a Vite dist/) is given.

    Routers reach shared state through `request.app.state`: `events` (the EventBus), `static_dir` and the library
    (`seedbox2.server.deps.get_library`: `library_path`, default library.sqlite3 in the support folder, opened on
    first use and closed on shutdown) and `jobs` (`seedbox2.server.deps.get_jobs`: the job Supervisor, started on
    first use, or with the server when `start_jobs`, so jobs left from the last run carry on; stopped on shutdown)
    and `local_music` (`seedbox2.server.deps.local_music`: imports, and the watch folders, scanned and watched from
    the start when `watch_folders`) and `models` (`seedbox2.server.deps.models`: the ModelManager, on `models_dir`,
    default models/ in the cache folder; with `download_models` it downloads the missing models when the server
    starts, unless the auto_download setting is off).
    """
    app = FastAPI(title="Seedbox 2", version=__version__, docs_url="/api/docs" if dev else None, redoc_url=None,
                  openapi_url="/api/openapi.json" if dev else None, lifespan=_lifespan)  # fmt: skip
    app.state.events = EventBus(heartbeat_s=heartbeat_s)
    app.state.static_dir = Path(static_dir) if static_dir is not None else None
    app.state.library_path = Path(library_path) if library_path is not None else None
    app.state.library = None
    app.state.jobs = None
    app.state.start_jobs = start_jobs
    app.state.local_music = None
    app.state.watch_folders = watch_folders
    app.state.models = None
    app.state.models_dir = Path(models_dir) if models_dir is not None else None
    app.state.download_models = download_models
    app.state.listen_trials = Trials()
    if dev:
        app.add_middleware(CORSMiddleware, allow_origins=DEV_ORIGINS, allow_methods=["*"], allow_headers=["*"])
    _add_error_handlers(app)
    for router in ROUTERS:
        app.include_router(router, prefix="/api")
    if static_dir is not None:
        _add_spa(app, Path(static_dir))
    elif not dev:
        _add_not_built_page(app)
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    if app.state.start_jobs:  # in the background: starting the server doesn't wait for the library
        from seedbox2.server.deps import start_jobs

        threading.Thread(target=start_jobs, args=(app,), name="seedbox2-jobs-start", daemon=True).start()
    if app.state.watch_folders:
        from seedbox2.server.deps import local_music

        threading.Thread(target=lambda: local_music(app).start_watching(), name="seedbox2-watch-start",
                         daemon=True).start()  # fmt: skip
    if app.state.download_models:
        from seedbox2.server.deps import models

        threading.Thread(target=lambda: models(app).start(), name="seedbox2-models-start", daemon=True).start()
    yield
    if app.state.models is not None:
        app.state.models.close()
        app.state.models = None
    if app.state.local_music is not None:
        app.state.local_music.close()
        app.state.local_music = None
    if app.state.jobs is not None:
        app.state.jobs.stop()
        app.state.jobs = None
    if app.state.library is not None:
        app.state.library.close()
        app.state.library = None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def _add_not_built_page(app: FastAPI) -> None:
    """Without a built frontend the window opens on this, not on a bare {"detail":"Not Found"}."""

    @app.get("/", include_in_schema=False)
    def not_built() -> Response:
        return HTMLResponse(NOT_BUILT_HTML)


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
