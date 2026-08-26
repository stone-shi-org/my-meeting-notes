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
from app.jobs.scheduler import AutoMatchScheduler, set_scheduler
from app.jobs.telegram_poller import TelegramPoller, set_poller
from app.routers import (
    auth,
    calendar,
    chat,
    dev,
    groups,
    home_chat,
    insight_types,
    insights,
    integrations,
    jobs,
    live_caption,
    matching,
    meeting_chat,
    meetings,
    notes,
    settings_api,
    summaries,
    system,
    threads,
    transcripts,
    users,
)
from app.services import pipeline  # noqa: F401  -- registers the job bodies
from app.services import diarize as diarize_svc
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
    diarize_svc.migrate_live_caption_backend_settings()
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

    # Always started, even with auto-match switched off: the loop asks the
    # setting on every tick, so an admin turning it on in the UI takes effect
    # within one tick rather than at the next restart.
    scheduler = AutoMatchScheduler()
    set_scheduler(scheduler)
    scheduler.start()

    # Same reasoning: always started, gated on telegram_enabled inside each
    # poll cycle rather than at startup.
    poller = TelegramPoller()
    set_poller(poller)
    poller.start()

    yield

    await poller.stop()
    set_poller(None)
    await scheduler.stop()
    set_scheduler(None)
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
    dist_resolved = dist.resolve()

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
        # vite's `public/` files (favicon, robots.txt, ...) land at the dist
        # root, not under /assets - serve them if the path matches one.
        if full_path:
            candidate = (dist / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(dist_resolved):
                return FileResponse(candidate)
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
    app.include_router(groups.router)
    app.include_router(groups.thread_router)
    app.include_router(meetings.router)
    app.include_router(transcripts.router)
    app.include_router(meeting_chat.router)
    app.include_router(summaries.router)
    app.include_router(matching.router)
    app.include_router(calendar.router)
    app.include_router(jobs.router)
    app.include_router(settings_api.router)
    app.include_router(chat.router)
    app.include_router(home_chat.router)
    app.include_router(notes.router)
    app.include_router(notes.meeting_router)
    app.include_router(dev.router)
    app.include_router(live_caption.router)
    app.include_router(insights.router)
    app.include_router(insight_types.router)

    _mount_spa(app, settings.web_dist)

    return app


app = create_app()
