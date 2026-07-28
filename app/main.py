"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import get_settings
from app.db import get_conn, init_db
from app.errors import register_exception_handlers
from app.logging_config import configure_logging, get_logger
from app.jobs.queue import JobQueue, set_queue
from app.routers import (
    auth,
    integrations,
    jobs,
    matching,
    meetings,
    settings_api,
    summaries,
    system,
    threads,
    transcripts,
    users,
)
from app.services import pipeline  # noqa: F401  -- registers the job bodies
from app.services import integrations as integrations_svc
from app.services import users as users_svc

log = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.audio_dir.mkdir(parents=True, exist_ok=True)

    init_db()
    with get_conn() as conn:
        users_svc.seed_admin(conn)
        purged = users_svc.purge_expired_sessions(conn)
    # After seed_admin, so a first boot migrates the seeded MCP config onto the
    # admin account rather than finding no users and doing nothing.
    integrations_svc.migrate_mcp_servers()
    if purged:
        log.info("purged %d expired session(s)", purged)
    log.info("database ready at %s", settings.db_path)

    if settings.session_cookie_secure is False:
        log.warning(
            "MMN_SESSION_COOKIE_SECURE=false - session cookies will be sent over "
            "plain HTTP. Set it to true when behind an HTTPS reverse proxy."
        )

    queue = JobQueue()
    set_queue(queue)
    # Reconcile before workers start, so a job left 'running' by a crash is
    # marked interrupted rather than being picked up twice.
    await queue.recover()
    await queue.start()

    yield

    await queue.stop()
    set_queue(None)
    log.info("shutdown complete")


def _mount_spa(app: FastAPI, dist: Path) -> None:
    """Serve the built SPA.

    Registered last so it can't shadow /api. Assets get immutable caching because
    Vite content-hashes their filenames; index.html must never be cached or a
    deploy leaves clients on a stale bundle.
    """
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    index = dist / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "not_found", "message": "Unknown API route"}},
            )
        if not index.exists():
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "spa_not_built",
                        "message": "web/dist/index.html is missing - run `npm run build` in web/",
                    }
                },
            )
        return FileResponse(index, headers={"Cache-Control": "no-store"})


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="My Meeting Notes",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    register_exception_handlers(app)

    app.include_router(system.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(integrations.router)
    app.include_router(threads.router)
    app.include_router(meetings.router)
    app.include_router(transcripts.router)
    app.include_router(summaries.router)
    app.include_router(matching.router)
    app.include_router(jobs.router)
    app.include_router(settings_api.router)

    _mount_spa(app, settings.web_dist)

    return app


app = create_app()
