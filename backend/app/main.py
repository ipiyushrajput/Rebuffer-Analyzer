"""FastAPI application.

Startup checks the database and the external binaries, loads stored thresholds, and resumes
any aging or bulk job that was running when the process last stopped.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.config import get_settings
from app.db import session as db_session
from app.logging_setup import configure_logging

logger = logging.getLogger("rba")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.rba_log_level)

    # Only what the deployment actually uses: sqlite keeps its file in the data directory,
    # and the reports directory exists only when a disk mirror is configured. Everything
    # else — reports, evidence archives, bulk bundles — lives in the database.
    directories = [settings.rba_data_dir] if settings.db_engine.lower() in ("sqlite", "") else []
    if settings.reports_on_disk:
        directories.append(settings.rba_reports_dir)
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

    mode = await db_session.ensure_database()
    logger.info("database bootstrap complete", extra={"schema_mode": mode})
    try:
        await db_session.create_all()
    except Exception as exc:
        logger.error("database schema creation failed: %s", type(exc).__name__)

    from app.api import settings as settings_api

    await settings_api.load_thresholds_from_db()

    from app.jobs.manager import job_manager
    from app.ws.hub import hub

    async def publish(job_id: str, kind: str, payload: dict[str, object]) -> None:
        await hub.publish(job_id, kind, dict(payload))

    job_manager.set_event_sink(publish)
    await job_manager.start()
    await job_manager.resume_persisted_jobs()

    try:
        yield
    finally:
        from app.api import proxy as proxy_api

        await job_manager.shutdown()
        await proxy_api.aclose()
        await db_session.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="TV Plus Rebuffer Analyzer",
        version=health.VERSION,
        description=(
            "Analyses linear HLS channels end to end and reports the exact defect, its "
            "evidence, the responsible party, and the fix."
        ),
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )

    from app.api import aging, bulk, cascada, catalogue, channels, proxy, realtime, reports
    from app.api import settings as settings_api
    from app.ws import routes as ws_routes

    app.include_router(health.router, prefix="/api")
    app.include_router(settings_api.router, prefix="/api")
    app.include_router(realtime.router, prefix="/api")
    app.include_router(aging.router, prefix="/api")
    app.include_router(bulk.router, prefix="/api")
    app.include_router(channels.router, prefix="/api")
    app.include_router(catalogue.router, prefix="/api")
    app.include_router(cascada.router, prefix="/api")
    app.include_router(reports.router, prefix="/api")
    app.include_router(proxy.router, prefix="/api")
    app.include_router(ws_routes.router)

    return app


app = create_app()
