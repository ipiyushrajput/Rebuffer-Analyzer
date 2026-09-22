"""Engine and session factory.

The connection string is built from `backend/.env` and never logged. `/api/health` reports
whether the database answers, and nothing about which host or user it answers as.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings
from app.db.models import Base

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# Set at bootstrap so the final summary and /api/health can say which storage option is in
# use without revealing credentials.
schema_mode: str = "unknown"


def describe_error(exc: BaseException) -> str:
    """A database failure an operator can act on, with the credentials taken out.

    The class name alone is not enough to act on: a driver that will not import, a host that
    refuses the connection and a password that is wrong all arrive as one word, and the
    operator is left reading tracebacks to find out which. The message says which — but a
    SQLAlchemy error can quote the connection URL, so the configured password is removed
    from it first, in both its raw and percent-encoded spellings.
    """
    from urllib.parse import quote_plus

    message = f"{type(exc).__name__}: {exc}".strip()
    password = get_settings().db_password
    if not password:
        return message
    for secret in (password, quote_plus(password)):
        if secret:
            message = message.replace(secret, "***")
    return message


def engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_recycle=1800,
            echo=False,
        )
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(engine(), expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def ensure_database() -> str:
    """Create the `rba` database when the user is allowed to, otherwise fall back.

    Returns the mode that was used: ``database``, ``schema``, ``table_prefix`` or ``sqlite``.
    The fallback never touches another application's tables — it only adds tables whose
    names start with the configured prefix.
    """
    global schema_mode
    settings = get_settings()
    engine_name = settings.db_engine.lower()

    if engine_name in ("sqlite", ""):
        schema_mode = "sqlite"
        return schema_mode

    try:
        server_engine = create_async_engine(
            settings.server_database_url, isolation_level="AUTOCOMMIT"
        )
        async with server_engine.connect() as connection:
            if engine_name in ("mysql", "mariadb"):
                await connection.execute(
                    text(
                        f"CREATE DATABASE IF NOT EXISTS `{settings.db_name}` "
                        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
                )
            else:
                exists = await connection.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"),
                    {"name": settings.db_name},
                )
                if exists.first() is None:
                    await connection.execute(text(f'CREATE DATABASE "{settings.db_name}"'))
        await server_engine.dispose()
        schema_mode = "database"
    except Exception as exc:
        logger.warning(
            "Dedicated database could not be created; RBA tables use the configured prefix. "
            "Reason: %s",
            describe_error(exc),
        )
        schema_mode = "table_prefix" if settings.db_table_prefix else "database"
    return schema_mode


async def create_all() -> None:
    async with engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


# The last schema comparison and when it was taken, so `/api/health` can report the drift
# without inspecting the database on every poll — and so an operator who runs the migration
# sees the badge clear without restarting the backend.
_schema_drift: dict[str, list[str]] = {}
_schema_checked_at: float = 0.0
SCHEMA_CHECK_TTL_S = 60.0


def _inspect_drift(connection: Any) -> dict[str, list[str]]:
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(connection)
    present = set(inspector.get_table_names())
    drift: dict[str, list[str]] = {}
    for table in Base.metadata.sorted_tables:
        if table.name not in present:
            # `create_all` makes a table that is absent, so this is not drift.
            continue
        have = {column["name"] for column in inspector.get_columns(table.name)}
        absent = [column.name for column in table.columns if column.name not in have]
        if absent:
            drift[table.name] = absent
    return drift


async def schema_drift(*, force: bool = False) -> dict[str, list[str]]:
    """Columns the code writes that the database does not have, by table.

    `create_all` creates a table that is missing and never alters one that is there, so a
    revision that adds a column leaves a deployment which has not run it with a schema the
    code writes against and the server rejects. What that looks like is one
    `OperationalError: Unknown column` per request and per batch of samples — thousands of
    lines of traceback, every measurement in those tables lost, and nothing anywhere saying
    that a migration is outstanding.

    Reported rather than repaired: Alembic owns the schema, the revision already exists, and
    applying DDL behind an operator's back is not something this project does. The answer is
    cached briefly so running the migration clears the report without a restart.
    """
    global _schema_drift, _schema_checked_at
    import time

    if not force and (time.monotonic() - _schema_checked_at) < SCHEMA_CHECK_TTL_S:
        return _schema_drift
    try:
        async with engine().connect() as connection:
            _schema_drift = await connection.run_sync(_inspect_drift)
    except Exception as exc:
        # A database that will not answer is already reported by `healthcheck`; this must not
        # turn into a second, competing story about the same outage.
        logger.warning("the schema could not be compared: %s", describe_error(exc))
        _schema_drift = {}
    _schema_checked_at = time.monotonic()
    return _schema_drift


def describe_drift(drift: dict[str, list[str]]) -> str:
    """The drift as one sentence, naming every column so the report is actionable."""
    return "; ".join(
        f"{table} is missing {', '.join(columns)}" for table, columns in sorted(drift.items())
    )


MIGRATION_COMMAND = (
    "run `make migrate`, or `cd backend && .venv/bin/python -m alembic upgrade head` "
    "(Windows: `deploy\\windows\\rba.cmd migrate`)"
)


async def healthcheck() -> dict[str, Any]:
    """Report reachability without revealing host, user, or database name."""
    try:
        async with engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
        return {"ok": True, "engine": get_settings().db_engine, "schema_mode": schema_mode}
    except Exception as exc:
        return {
            "ok": False,
            "engine": get_settings().db_engine,
            "schema_mode": schema_mode,
            "error": describe_error(exc),
        }


async def dispose() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
