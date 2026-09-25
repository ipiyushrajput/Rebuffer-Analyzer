"""Reading the newest rows of a wide table without sorting the wide part.

MySQL's filesort packs every selected column into the sort buffer. A `jobs` row carries two
JSON columns and five TEXT ones, and a `batches` row carries its settings snapshot, so
ordering those tables by `created_at` asks the server to hold the whole result set — verdicts,
parameters, URLs and all — in `sort_buffer_size` bytes. Past a few hundred rows it does not
fit and the query fails outright with error 1038, "Out of sort memory": a listing that worked
on a fresh deployment stops working once it has some history, and stops working for everyone
at once.

Sorting a projection of the primary key alone keeps each sort row at a handful of bytes
whatever the table holds. The second query then fetches those rows by key, which needs no
sort at all. Two round trips, one bounded by the limit, in exchange for a listing whose cost
no longer depends on how wide the table is or how the server is tuned.

The `created_at` indexes added alongside this let the server read in order and stop at the
limit, which usually removes the sort as well. This module is what holds when it does not —
an unindexed column, a filter the optimiser reads differently, a server configured smaller
than the one it was tested on.
"""

from __future__ import annotations

from typing import Any, TypeVar

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.inspection import inspect

from app.db.models import Base

ModelT = TypeVar("ModelT", bound=Base)

# How many keys one `IN (...)` carries when rows are fetched back by key.
KEY_CHUNK = 500


async def newest_rows(
    session: AsyncSession,
    model: type[ModelT],
    *,
    where: tuple[ColumnElement[bool], ...] = (),
    order_by: tuple[Any, ...],
    limit: int | None,
) -> list[ModelT]:
    """The first `limit` rows in `order_by` order, sorted on the primary key alone.

    The rows come back in the order asked for: the key query decides it, and the row query is
    re-ordered against that rather than sorted a second time. `limit=None` returns every
    matching row, still sorted on the key alone — a batch's channels, a batch's log.
    """
    key = inspect(model).primary_key[0]

    ordered = select(key).where(*where).order_by(*order_by, key)
    if limit is not None:
        ordered = ordered.limit(limit)
    keys = list((await session.execute(ordered)).scalars().all())
    if not keys:
        return []

    rows: list[ModelT] = []
    # Fetched by key in slices, so a long IN list stays well inside any server's limits.
    for start in range(0, len(keys), KEY_CHUNK):
        chunk = keys[start : start + KEY_CHUNK]
        rows.extend((await session.execute(select(model).where(key.in_(chunk)))).scalars().all())
    position = {value: index for index, value in enumerate(keys)}
    rows.sort(key=lambda row: position[getattr(row, key.name)])
    return rows


async def sorted_rows(
    session: AsyncSession,
    model: type[ModelT],
    *,
    where: tuple[ColumnElement[bool], ...] = (),
    order_by: tuple[Any, ...],
) -> list[ModelT]:
    """Every matching row in `order_by` order, the sort run on the primary key alone.

    For a child table read whole — a batch's channels carry TEXT and a JSON correlation that
    grows with the spikes a week had, so sorting the rows themselves fails with error 1038
    exactly when a batch has the most to report.
    """
    return await newest_rows(session, model, where=where, order_by=order_by, limit=None)
