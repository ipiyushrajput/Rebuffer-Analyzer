"""FastAPI application.

Startup checks the database and the external binaries, loads stored thresholds, resumes any
aging or bulk job that was running when the process last stopped, picks up any automated
batch left mid-run, and starts the weekly batch scheduler.
"""

from __future__ import annotations

import asyncio
import contextlib
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
        # Without the reason an operator cannot tell a missing driver from a refused
        # connection, and every later request fails the same way with no explanation.
        logger.error("database schema creation failed: %s", db_session.describe_error(exc))

    from app.api import settings as settings_api

    await settings_api.load_thresholds_from_db()
    # Severities an operator reassigned are in force before the first job starts, so a run
    # never files findings under a classification the deployment has already changed.
    await settings_api.load_rule_severities_from_db()

    from app.jobs.manager import job_manager
    from app.ws.hub import hub

    async def publish(job_id: str, kind: str, payload: dict[str, object]) -> None:
        await hub.publish(job_id, kind, dict(payload))

    job_manager.set_event_sink(publish)
    await job_manager.start()
    await job_manager.resume_persisted_jobs()

    # Automated batches. The sweep runs first so a batch this process was running when it
    # stopped is picked up rather than left in a running state with nothing running it; the
    # scheduler then fires whatever the weekly schedule is due for.
    from app.batch import runner as batch_runner
    from app.batch import scheduler as batch_scheduler

    try:
        await batch_runner.sweep_unfinished()
    except Exception as exc:
        logger.warning("unfinished batches were not swept: %s", db_session.describe_error(exc))
    # Awaited here rather than inside the loop: the default schedule is in place before the
    # server takes its first request, so it cannot race one that writes the same row.
    await batch_scheduler.ensure_default()
    scheduler_task = asyncio.create_task(batch_scheduler.loop(), name="rba:batch-scheduler")

    try:
        yield
    finally:
        from app.api import proxy as proxy_api

        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
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

    from app.api import aging, batch, bulk, cascada, catalogue, channels, proxy, realtime, reports
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
    app.include_router(batch.router, prefix="/api")
    app.include_router(reports.router, prefix="/api")
    app.include_router(proxy.router, prefix="/api")
    app.include_router(ws_routes.router)

    return app


app = create_app()
