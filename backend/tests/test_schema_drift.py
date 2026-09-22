"""What happens when the database is a migration behind the code.

`create_all` at startup adds a table that is missing and never alters one that is already
there. A revision that adds a column therefore does nothing until somebody runs it — and
until then the code writes a column the server does not have. On MySQL that is:

    pymysql.err.OperationalError: (1054, "Unknown column 'bandwidth_bps' in 'field list'")

once per request that reads the table and once per batch of samples that writes it. It
reached a deployment exactly that way: thousands of lines of traceback, ninety-five player
samples lost per batch, and nothing anywhere saying a migration was outstanding.

It is reported, not repaired. Alembic owns the schema and the revision already exists;
applying DDL behind an operator's back is not something this project does. What changes is
that the drift is named — at startup, on `/api/health`, and so in the rail — with the columns
and the command.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import session as db_session
from app.main import create_app


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    """The answer is cached for a minute so the rail clears without a restart; a test that
    changes the schema has to see its own change."""
    db_session._schema_checked_at = 0.0
    db_session._schema_drift = {}


@contextlib.asynccontextmanager
async def _without_column(table: str, column: str, ddl_type: str) -> AsyncIterator[None]:
    """Reproduce a schema a migration behind, and put it back.

    The scratch database outlives one test, so a column dropped and left dropped makes every
    later test in the file read as drift it did not cause.
    """
    async with db_session.engine().begin() as connection:
        await connection.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    db_session._schema_checked_at = 0.0
    try:
        yield
    finally:
        async with db_session.engine().begin() as connection:
            await connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        db_session._schema_checked_at = 0.0


@pytest.mark.asyncio()
async def test_a_schema_that_matches_the_models_reports_no_drift() -> None:
    with TestClient(create_app()):
        drift = await db_session.schema_drift(force=True)

    assert drift == {}


@pytest.mark.asyncio()
async def test_a_missing_column_is_named_with_its_table() -> None:
    """The column from the live failure, dropped to reproduce it."""
    with TestClient(create_app()):
        async with _without_column("samples_player", "bandwidth_bps", "BIGINT"):
            drift = await db_session.schema_drift(force=True)

    assert drift == {"samples_player": ["bandwidth_bps"]}


@pytest.mark.asyncio()
async def test_the_drift_reads_as_a_sentence_naming_every_column() -> None:
    described = db_session.describe_drift({"samples_player": ["bandwidth_bps"], "jobs": ["a", "b"]})

    assert "samples_player is missing bandwidth_bps" in described
    assert "jobs is missing a, b" in described


def test_the_command_that_clears_it_is_named_for_both_platforms() -> None:
    """An operator reading the rail has to be told what to run, not just what is wrong."""
    command = db_session.MIGRATION_COMMAND

    assert "alembic upgrade head" in command
    assert "make migrate" in command
    assert "rba.cmd migrate" in command


@pytest.mark.asyncio()
async def test_health_degrades_on_drift_and_says_which_columns() -> None:
    with TestClient(create_app()) as client:
        async with _without_column("samples_player", "bandwidth_bps", "BIGINT"):
            body = client.get("/api/health").json()

    assert body["status"] == "degraded"
    assert "schema" in body["degraded"]
    check = body["checks"]["schema"]
    assert check["ok"] is False
    assert check["missing"] == {"samples_player": ["bandwidth_bps"]}
    assert "bandwidth_bps" in check["detail"]
    assert "alembic upgrade head" in check["detail"]


def test_health_is_not_degraded_by_the_schema_when_it_matches() -> None:
    with TestClient(create_app()) as client:
        body = client.get("/api/health").json()

    assert "schema" not in body["degraded"]
    assert body["checks"]["schema"]["ok"] is True


@pytest.mark.asyncio()
async def test_a_database_that_will_not_answer_does_not_become_a_second_outage_story(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`healthcheck` already reports an unreachable database. This must not compete with it
    by claiming every column is missing as well."""

    def explode() -> None:
        raise RuntimeError("no route to host")

    with TestClient(create_app()):
        monkeypatch.setattr(db_session, "engine", explode)
        drift = await db_session.schema_drift(force=True)

    assert drift == {}
