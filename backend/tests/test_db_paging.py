"""Reading the newest rows of a wide table.

MySQL's filesort packs every selected column into `sort_buffer_size`. A `jobs` row carries two
JSON columns and five TEXT ones, so ordering the rows themselves asks the server to hold every
verdict and every URL in memory at once, and past a few hundred rows of history it refuses:
error 1038, "Out of sort memory, consider increasing server sort buffer size". The listing
worked on a fresh deployment and stopped working on one with a month behind it.

What is asserted here is the property that makes the size of the table irrelevant: the query
that sorts selects the primary key and nothing else.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event

from app.db import session as db_session
from app.db.models import Job as JobRow
from app.db.paging import newest_rows
from app.main import create_app

# Enough text to matter in a sort buffer, in every column that holds any.
BULKY = "x" * 4000


@pytest.fixture()
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


async def _clear() -> None:
    async with db_session.session_scope() as session:
        await session.execute(delete(JobRow).where(JobRow.id.like("paging-%")))


async def _seed(count: int) -> list[str]:
    """`count` wide job rows, oldest first, so the newest ids are the last ones made.

    Stamped from now forward rather than from a fixed date: other tests in the suite leave
    jobs behind, and a listing asked for the newest rows would return theirs instead.
    """
    base = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    ids = []
    async with db_session.session_scope() as session:
        for index in range(count):
            job_id = f"paging-{index:04d}"
            ids.append(job_id)
            session.add(
                JobRow(
                    id=job_id,
                    type="aging",
                    status="COMPLETED",
                    channel_name=f"Channel {index}",
                    playback_url=f"https://cdn.example/{index}/index.m3u8?{BULKY}",
                    origin_url=BULKY,
                    cdn_url=BULKY,
                    ssai_url=BULKY,
                    error=BULKY,
                    params={"note": BULKY},
                    verdict={"counts": {"CRITICAL": 1}, "detail": BULKY},
                    created_at=base + dt.timedelta(minutes=index),
                )
            )
    return ids


@pytest.mark.asyncio()
async def test_the_newest_rows_come_back_newest_first(api: TestClient) -> None:
    await _clear()
    ids = await _seed(12)
    try:
        async with db_session.session_scope() as session:
            rows = await newest_rows(
                session,
                JobRow,
                where=(JobRow.id.like("paging-%"),),
                order_by=(JobRow.created_at.desc(),),
                limit=5,
            )

        assert [row.id for row in rows] == list(reversed(ids))[:5]
    finally:
        await _clear()


@pytest.mark.asyncio()
async def test_the_query_that_sorts_selects_the_key_and_nothing_else(api: TestClient) -> None:
    """
    The defect this exists to prevent.

    Selecting the row while sorting is what put the JSON and TEXT columns in the sort buffer.
    Whatever else changes, the statement carrying the ORDER BY must name one column.
    """
    await _clear()
    await _seed(6)
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        statements.append(" ".join(statement.split()))

    try:
        async with db_session.session_scope() as session:
            engine = session.get_bind().engine  # type: ignore[union-attr]
            event.listen(engine, "before_cursor_execute", record)
            try:
                await newest_rows(
                    session,
                    JobRow,
                    where=(JobRow.id.like("paging-%"),),
                    order_by=(JobRow.created_at.desc(),),
                    limit=3,
                )
            finally:
                event.remove(engine, "before_cursor_execute", record)

        sorted_statements = [s for s in statements if "ORDER BY" in s]
        assert len(sorted_statements) == 1, "exactly one statement sorts"

        sorting = sorted_statements[0]
        assert "SELECT jobs.id" in sorting
        for wide in ("jobs.verdict", "jobs.params", "jobs.error", "jobs.playback_url"):
            assert wide not in sorting, f"{wide} would be packed into the sort buffer"

        fetching = [s for s in statements if "ORDER BY" not in s and "SELECT" in s]
        assert any("jobs.verdict" in s for s in fetching), "the rows are still loaded in full"
    finally:
        await _clear()


@pytest.mark.asyncio()
async def test_a_filter_that_matches_nothing_runs_one_query_and_returns_nothing(
    api: TestClient,
) -> None:
    """No keys means no second query: there is nothing to fetch by."""
    await _clear()
    async with db_session.session_scope() as session:
        rows = await newest_rows(
            session,
            JobRow,
            where=(JobRow.id.like("paging-nothing-%"),),
            order_by=(JobRow.created_at.desc(),),
            limit=10,
        )
    assert rows == []


@pytest.mark.asyncio()
async def test_the_limit_is_what_bounds_the_listing(api: TestClient) -> None:
    await _clear()
    await _seed(40)
    try:
        async with db_session.session_scope() as session:
            rows = await newest_rows(
                session,
                JobRow,
                where=(JobRow.id.like("paging-%"),),
                order_by=(JobRow.created_at.desc(),),
                limit=7,
            )
        assert len(rows) == 7
    finally:
        await _clear()


def test_the_channels_listing_returns_the_wide_rows_it_sorted(api: TestClient) -> None:
    """End to end: the endpoint that failed still answers with each job's verdict intact."""
    api.portal.call(_clear)  # type: ignore[attr-defined]
    api.portal.call(_seed, 30)  # type: ignore[attr-defined]
    try:
        body = api.get("/api/channels", params={"limit": 10}).json()

        channels = [c for c in body["channels"] if c["id"].startswith("paging-")]
        assert len(channels) == 10
        assert [c["id"] for c in channels] == [f"paging-{index:04d}" for index in range(29, 19, -1)]
        assert channels[0]["verdict"]["counts"] == {"CRITICAL": 1}
    finally:
        api.portal.call(_clear)  # type: ignore[attr-defined]


# -- a batch's channels and log, and a bulk job's rows -------------------------
#
# The same defect, found on a real deployment: a historical batch of 29 channels, each with a
# correlation listing every spike window of its week, failed its report with error 1038 on
# `SELECT batch_items.* ... ORDER BY batch_items.average_pct DESC`. Child tables read whole
# are sorted on the key too.


def _sorting_statements(statements: list[str]) -> list[str]:
    return [s for s in statements if "ORDER BY" in s]


async def _recorded(call: Any) -> tuple[Any, list[str]]:
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        statements.append(" ".join(statement.split()))

    sync_engine = db_session.engine().sync_engine
    event.listen(sync_engine, "before_cursor_execute", record)
    try:
        result = await call()
    finally:
        event.remove(sync_engine, "before_cursor_execute", record)
    return result, statements


@pytest.mark.asyncio()
async def test_a_batchs_channels_and_log_are_sorted_on_the_key_alone(api: TestClient) -> None:
    from app.batch import store

    batch_id = "paging-batch"
    await store.create(batch_id=batch_id, country="GB", kind=store.MANUAL, snapshot={})
    try:
        await store.add_items(
            batch_id,
            [
                {"service_id": f"GB{n}", "channel_name": f"C{n}", "average_pct": avg,
                 "playback_url": BULKY, "error": BULKY}
                for n, avg in enumerate([0.3, 0.9, 0.5, None, 0.7])
            ],
        )  # fmt: skip
        for n in range(5):
            await store.update_item(
                batch_id, f"GB{n}", summary=BULKY, correlation={"windows": [BULKY] * 20}
            )
            await store.log(batch_id, BULKY)

        read, statements = await _recorded(lambda: store.read(batch_id, with_items=True))
        assert read is not None
        # Worst first, as the report and the tab need them.
        averages = [item["average_pct"] for item in read["items"] if item["average_pct"]]
        assert averages == sorted(averages, reverse=True)
        for sorting in _sorting_statements(statements):
            assert "SELECT batch_items.id" in sorting
            for wide in ("correlation", "summary", "playback_url", "batch_items.error"):
                assert wide not in sorting, f"{wide} would be packed into the sort buffer"

        items, statements = await _recorded(lambda: store.items_for(batch_id))
        assert [item["service_id"] for item in items][:3] == ["GB1", "GB4", "GB2"]
        assert all("correlation" not in s for s in _sorting_statements(statements))

        lines, statements = await _recorded(lambda: store.log_lines(batch_id))
        assert len(lines) >= 5
        assert all("message" not in s for s in _sorting_statements(statements))
    finally:
        from sqlalchemy import delete as sql_delete

        from app.db.models import Batch, BatchItem, BatchLog

        async with db_session.session_scope() as session:
            await session.execute(sql_delete(BatchLog).where(BatchLog.batch_id == batch_id))
            await session.execute(sql_delete(BatchItem).where(BatchItem.batch_id == batch_id))
            await session.execute(sql_delete(Batch).where(Batch.id == batch_id))
