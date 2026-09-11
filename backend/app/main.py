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

    for directory in (settings.rba_data_dir, settings.rba_reports_dir, settings.rba_evidence_dir):
        directory.mkdir(parents=True, exist_ok=True)

    mode = await db_session.ensure_database()
    logger.info("database bootstrap complete", extra={"schema_mode": mode})
    try:
        await db_session.create_all()
    except Exception as exc:
        logger.error("database schema creation failed: %s", type(exc).__name__)

    from app.api import settings as settings_api

    await settings_api.load_thresholds_from_db()

    try:
        yield
    finally:
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

    from app.api import settings as settings_api

    app.include_router(health.router, prefix="/api")
    app.include_router(settings_api.router, prefix="/api")

    return app


app = create_app()
