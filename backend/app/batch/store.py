"""Reading and writing a batch.

Every phase of a batch writes here before it does anything else, which is what makes the run
independent of the browser that started it: progress after a reload, a resume after a
restart, and a report that can be regenerated without re-running a single channel all read
from these rows.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import func, select

from app.db import session as db_session
from app.db.models import Batch, BatchItem, BatchLog
from app.db.paging import newest_rows

logger = logging.getLogger(__name__)

# The states a batch passes through. `phase` is the finer-grained step inside a run, which is
# what makes "Analysing 12 / 37" possible and what a resume re-enters at.
QUEUED = "QUEUED"
SCANNING = "SCANNING"
ANALYSING = "ANALYSING"
REPORTING = "REPORTING"
COMPLETED = "COMPLETED"
COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

RUNNING_STATES = (QUEUED, SCANNING, ANALYSING, REPORTING)
FINISHED_STATES = (COMPLETED, COMPLETED_WITH_ERRORS, FAILED, CANCELLED)

MANUAL = "manual"
SCHEDULED = "scheduled"


def _utc(moment: dt.datetime | None) -> dt.datetime | None:
    """A stored timestamp as UTC. SQLite hands back naive values; MySQL does not."""
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def item_payload(row: BatchItem) -> dict[str, Any]:
    return {
        "service_id": row.service_id,
        "channel_name": row.channel_name,
        "country": row.country or "",
        "playback_url": row.playback_url or "",
        "average_pct": row.average_pct,
        "max_pct": row.max_pct,
        "minutes_above": row.minutes_above,
        "status": row.status,
        "job_id": row.job_id,
        "aging_job_id": row.aging_job_id,
        "error": row.error,
        "summary": row.summary,
        "correlation": row.correlation or {},
    }


def batch_payload(row: Batch, items: list[BatchItem] | None = None) -> dict[str, Any]:
    """One batch as Playground reads it."""
    window_from = (
        _utc(dt.datetime.fromtimestamp(row.window_from, dt.UTC)) if row.window_from else None
    )
    window_to = _utc(dt.datetime.fromtimestamp(row.window_to, dt.UTC)) if row.window_to else None
    started = _utc(row.started_at)
    finished = _utc(row.finished_at)
    payload: dict[str, Any] = {
        "id": row.id,
        "country": row.country,
        "kind": row.kind,
        "status": row.status,
        "phase": row.phase,
        "running": row.status in RUNNING_STATES,
        "channels_listed": row.channels_listed,
        "channels_scanned": row.channels_scanned,
        "channels_above": row.channels_above,
        "channels_analysed": row.channels_analysed,
        "channels_failed": row.channels_failed,
        # The window the averages cover, which is also the report's column label.
        "window_from": window_from.isoformat() if window_from else None,
        "window_to": window_to.isoformat() if window_to else None,
        "created_at": (_utc(row.created_at) or dt.datetime.now(dt.UTC)).isoformat(),
        "started_at": started.isoformat() if started else None,
        "finished_at": finished.isoformat() if finished else None,
        "error": row.error,
        "settings": row.settings_snapshot or {},
        # Recorded in the frozen settings when the batch started. A batch from before there
        # was a choice read realtime, which is what it says.
        "data_source": (row.settings_snapshot or {}).get("data_source", "realtime"),
    }
    if items is not None:
        payload["items"] = [item_payload(item) for item in items]
    return payload


async def create(
    *, batch_id: str, country: str, kind: str, snapshot: dict[str, Any]
) -> dict[str, Any]:
    async with db_session.session_scope() as session:
        row = Batch(
            id=batch_id,
            country=country,
            kind=kind,
            status=QUEUED,
            phase=QUEUED,
            settings_snapshot=snapshot,
            created_at=dt.datetime.now(dt.UTC),
        )
        session.add(row)
        session.add(
            BatchLog(
                batch_id=batch_id,
                at=dt.datetime.now(dt.UTC),
                level="INFO",
                message=f"Batch queued for {country} ({kind}).",
            )
        )
    return {"id": batch_id, "country": country, "kind": kind, "status": QUEUED}


async def log(batch_id: str, message: str, level: str = "INFO") -> None:
    """One line of what the batch did. Failing to log must never fail the batch."""
    try:
        async with db_session.session_scope() as session:
            session.add(
                BatchLog(
                    batch_id=batch_id, at=dt.datetime.now(dt.UTC), level=level, message=message
                )
            )
    except Exception as exc:
        logger.warning("batch %s log line was not stored: %s", batch_id, type(exc).__name__)


async def update(batch_id: str, **fields: Any) -> None:
    """Write a batch's progress. Called after every phase and every channel."""
    async with db_session.session_scope() as session:
        row = await session.get(Batch, batch_id)
        if row is None:
            return
        for name, value in fields.items():
            setattr(row, name, value)


async def read(batch_id: str, with_items: bool = False) -> dict[str, Any] | None:
    async with db_session.session_scope() as session:
        row = await session.get(Batch, batch_id)
        if row is None:
            return None
        items: list[BatchItem] = []
        if with_items:
            items = list(
                (
                    await session.execute(
                        select(BatchItem)
                        .where(BatchItem.batch_id == batch_id)
                        .order_by(BatchItem.average_pct.desc())
                    )
                )
                .scalars()
                .all()
            )
        return batch_payload(row, items if with_items else None)


async def listing(country: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Every batch, newest first. Playground's list."""
    async with db_session.session_scope() as session:
        # Sorted on the primary key alone; see `app/db/paging.py`. A batch row carries its
        # settings snapshot, so ordering the rows themselves grows with the history.
        where: tuple[Any, ...] = (Batch.country == country.upper(),) if country else ()
        rows = await newest_rows(
            session,
            Batch,
            where=where,
            order_by=(Batch.created_at.desc(),),
            limit=limit,
        )
        return [batch_payload(row) for row in rows]


async def running_for(country: str) -> dict[str, Any] | None:
    """The batch already in flight for a country, if there is one.

    Two batches for one country would scan the same channels and analyse them twice, so the
    second is refused and the first is offered instead.
    """
    async with db_session.session_scope() as session:
        row = (
            (
                await session.execute(
                    select(Batch)
                    .where(Batch.country == country.upper(), Batch.status.in_(RUNNING_STATES))
                    .order_by(Batch.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        return batch_payload(row) if row is not None else None


async def add_items(batch_id: str, items: list[dict[str, Any]]) -> None:
    """Record the channels a batch selected, before any of them is analysed."""
    async with db_session.session_scope() as session:
        for item in items:
            session.add(
                BatchItem(
                    batch_id=batch_id,
                    service_id=item["service_id"],
                    channel_name=item["channel_name"],
                    country=item.get("country"),
                    playback_url=item.get("playback_url"),
                    average_pct=item.get("average_pct"),
                    max_pct=item.get("max_pct"),
                    minutes_above=int(item.get("minutes_above") or 0),
                    status=item.get("status", "PENDING"),
                    error=item.get("error"),
                )
            )


async def update_item(batch_id: str, service_id: str, **fields: Any) -> None:
    async with db_session.session_scope() as session:
        row = (
            (
                await session.execute(
                    select(BatchItem).where(
                        BatchItem.batch_id == batch_id, BatchItem.service_id == service_id
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return
        for name, value in fields.items():
            setattr(row, name, value)


async def items_for(batch_id: str, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """A batch's channels, worst first. `statuses` narrows to the ones still to do."""
    async with db_session.session_scope() as session:
        query = (
            select(BatchItem)
            .where(BatchItem.batch_id == batch_id)
            .order_by(BatchItem.average_pct.desc())
        )
        if statuses:
            query = query.where(BatchItem.status.in_(statuses))
        rows = list((await session.execute(query)).scalars().all())
        return [item_payload(row) for row in rows]


async def log_lines(batch_id: str, limit: int = 2000) -> list[dict[str, str]]:
    async with db_session.session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(BatchLog)
                    .where(BatchLog.batch_id == batch_id)
                    .order_by(BatchLog.at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "at": (_utc(row.at) or dt.datetime.now(dt.UTC)).isoformat(),
                "level": row.level,
                "message": row.message,
            }
            for row in rows
        ]


async def unfinished() -> list[dict[str, Any]]:
    """Batches the process was running when it stopped. The startup sweep reads this."""
    async with db_session.session_scope() as session:
        rows = list(
            (await session.execute(select(Batch).where(Batch.status.in_(RUNNING_STATES))))
            .scalars()
            .all()
        )
        return [batch_payload(row) for row in rows]


async def count_for(country: str) -> int:
    async with db_session.session_scope() as session:
        total = await session.scalar(
            select(func.count(Batch.id)).where(Batch.country == country.upper())
        )
        return int(total or 0)


async def previous_batch(country: str, before: str) -> dict[str, Any] | None:
    """The batch before this one for a country, which is where last week's aging runs are."""
    async with db_session.session_scope() as session:
        current = await session.get(Batch, before)
        if current is None:
            return None
        row = (
            (
                await session.execute(
                    select(Batch)
                    .where(
                        Batch.country == country.upper(),
                        Batch.created_at < current.created_at,
                    )
                    .order_by(Batch.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        return batch_payload(row) if row is not None else None
