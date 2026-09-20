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


async def newest_rows(
    session: AsyncSession,
    model: type[ModelT],
    *,
    where: tuple[ColumnElement[bool], ...] = (),
    order_by: tuple[Any, ...],
    limit: int,
) -> list[ModelT]:
    """The first `limit` rows in `order_by` order, sorted on the primary key alone.

    The rows come back in the order asked for: the key query decides it, and the row query is
    re-ordered against that rather than sorted a second time.
    """
    key = inspect(model).primary_key[0]

    keys = list(
        (await session.execute(select(key).where(*where).order_by(*order_by).limit(limit)))
        .scalars()
        .all()
    )
    if not keys:
        return []

    rows = list((await session.execute(select(model).where(key.in_(keys)))).scalars().all())
    position = {value: index for index, value in enumerate(keys)}
    rows.sort(key=lambda row: position[getattr(row, key.name)])
    return rows
