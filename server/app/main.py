"""Application entry point.

Run it with::

    uvicorn app.main:app --reload --app-dir server
"""
from __future__ import annotations

import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import SessionLocal, init_db
from .ratelimit import RateLimiter
from .realtime.hub import RoomRegistry
from .realtime.ws import router as ws_router
from .routers import (
    assistant,
    attribution,
    auth,
    collaborators,
    documents,
    files,
    integrity,
    me,
    notifications,
    oauth,
    pages,
    public,
    security,
    workspace,
)

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
SERVED_BY = socket.gethostname()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as db:
        documents.purge_trash(db)
    app.state.rooms = RoomRegistry(get_settings())
    await app.state.rooms.start()
    try:
        yield
    finally:
        # Drains every open room's operation queue, so a shutdown mid-edit
        # does not lose the last second of typing.
        await app.state.rooms.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Collaborative Document Server with AI Integration",
        description=(
            "Real-time collaborative editing with operational transformation "
            "and stylometric authorship attribution."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.limiter = RateLimiter()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def timing_header(request: Request, call_next):
        """Expose server-side handling time (SRS 5.1 latency budget)."""
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Process-Time-Ms"] = "%.2f" % (
            (time.perf_counter() - started) * 1000
        )
        # Defence in depth for every response; routes that need stricter
        # rules (the published pages) set their own and are not overridden.
        # Which replica answered: behind the router, requests for one
        # document should always name the same pod.
        response.headers["X-Served-By"] = SERVED_BY
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response

    app.include_router(auth.router)
    app.include_router(oauth.router)
    app.include_router(assistant.router)
    app.include_router(documents.router)
    app.include_router(collaborators.router)
    app.include_router(attribution.router)
    app.include_router(integrity.router)
    app.include_router(workspace.router)
    app.include_router(notifications.router)
    app.include_router(files.router)
    app.include_router(me.router)
    app.include_router(public.router)
    app.include_router(pages.router)
    app.include_router(security.router)
    app.include_router(ws_router)

    @app.get("/api/health", tags=["ops"])
    async def health(request: Request) -> JSONResponse:
        registry: RoomRegistry = request.app.state.rooms
        return JSONResponse(
            {
                "status": "ok",
                "broadcaster": get_settings().broadcaster,
                "open_documents": registry.open_documents,
            }
        )

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

        # The service worker must be served from the root to control the
        # whole app, and must not be cached by the browser's HTTP cache, or
        # a fixed worker could not replace a broken one.
        @app.get("/sw.js", include_in_schema=False)
        async def service_worker() -> FileResponse:
            return FileResponse(
                WEB_DIR / "sw.js",
                media_type="text/javascript",
                headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
            )

        @app.get("/manifest.webmanifest", include_in_schema=False)
        async def manifest() -> FileResponse:
            return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    return app


app = create_app()
