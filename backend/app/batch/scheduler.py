"""The weekly firing, and why it is a row rather than a timer.

A schedule held in the process dies with the process, and a deployment that restarts on a
Sunday evening would silently miss Monday. So the schedule is a `batch_schedules` row, the
loop below only reads it, and `last_success_at` — also a row — is what the seven-day gap is
measured from. A restart loses nothing.

Two things must never happen: two batches for one country at once, and a second run inside
the same week. Both are checked before firing and both are written down when they stop a
firing, because a schedule that skips silently is indistinguishable from one that is broken.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.batch import runner, store
from app.db import session as db_session
from app.db.models import BatchSchedule

logger = logging.getLogger(__name__)

# How often the loop looks at the clock. A minute is fine: the schedule has minute precision
# and a firing that lands a minute late is a firing.
TICK_S = 60.0

# A firing is allowed inside this many minutes after its time, so a tick that lands late — or
# a process that was restarting at exactly the wrong moment — still fires.
GRACE_MINUTES = 30

# The gap a country must leave between successful runs.
MIN_DAYS_BETWEEN_RUNS = 7

MONDAY = 0
DEFAULT_HOUR_UTC = 2


def _utc(moment: dt.datetime | None) -> dt.datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def schedule_payload(row: BatchSchedule) -> dict[str, Any]:
    fired = _utc(row.last_fired_at)
    success = _utc(row.last_success_at)
    return {
        "country": row.country,
        "enabled": row.enabled,
        "weekday": row.weekday,
        "hour_utc": row.hour_utc,
        "minute_utc": row.minute_utc,
        "overrides": row.overrides or {},
        "last_fired_at": fired.isoformat() if fired else None,
        "last_success_at": success.isoformat() if success else None,
        "last_reason": row.last_reason,
    }


def due_at(row: BatchSchedule, now: dt.datetime) -> bool:
    """Whether this schedule's time has arrived, within the grace window.

    The check is "is it this weekday, and has its time passed within the grace window", not
    "is it exactly this minute": a tick that lands late must still fire rather than waiting
    another week.
    """
    if not row.enabled:
        return False
    if now.weekday() != row.weekday:
        return False
    fires_at = now.replace(hour=row.hour_utc, minute=row.minute_utc, second=0, microsecond=0)
    if now < fires_at:
        return False
    return (now - fires_at) <= dt.timedelta(minutes=GRACE_MINUTES)


def too_soon(row: BatchSchedule, now: dt.datetime) -> bool:
    """Whether fewer than seven days have passed since the last successful run."""
    last = _utc(row.last_success_at)
    if last is None:
        return False
    return (now - last) < dt.timedelta(days=MIN_DAYS_BETWEEN_RUNS)


async def list_schedules() -> list[dict[str, Any]]:
    async with db_session.session_scope() as session:
        rows = (
            (await session.execute(select(BatchSchedule).order_by(BatchSchedule.country)))
            .scalars()
            .all()
        )
        return [schedule_payload(row) for row in rows]


async def _write_schedule(
    code: str,
    enabled: bool,
    weekday: int,
    hour_utc: int,
    minute_utc: int,
    overrides: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """One read-then-write attempt. `None` means the row appeared in between."""
    try:
        async with db_session.session_scope() as session:
            row = await session.get(BatchSchedule, code)
            if row is None:
                row = BatchSchedule(country=code)
                session.add(row)
            row.enabled = enabled
            row.weekday = max(0, min(6, weekday))
            row.hour_utc = max(0, min(23, hour_utc))
            row.minute_utc = max(0, min(59, minute_utc))
            row.overrides = overrides or {}
            await session.flush()
            return schedule_payload(row)
    except IntegrityError:
        return None


async def save_schedule(
    *,
    country: str,
    enabled: bool,
    weekday: int,
    hour_utc: int,
    minute_utc: int,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or retime one country's weekly firing.

    The read and the write are two statements, so the row can be created between them — by
    `ensure_default` on a host that has just started, by a second operator, or by a second
    analyzer instance against the same database. The insert then collides with the primary
    key. The second attempt reads the row that won and updates it, which is what the caller
    asked for, so the race costs a statement rather than a 500.
    """
    code = country.upper()
    for _ in range(2):
        written = await _write_schedule(code, enabled, weekday, hour_utc, minute_utc, overrides)
        if written is not None:
            return written
    raise IntegrityError(  # pragma: no cover - two collisions on one primary key
        f"The schedule for {code} could not be written.", (), Exception(code)
    )


async def delete_schedule(country: str) -> bool:
    async with db_session.session_scope() as session:
        row = await session.get(BatchSchedule, country.upper())
        if row is None:
            return False
        await session.delete(row)
        return True


async def ensure_default() -> None:
    """GB runs every Monday at 02:00 UTC unless something else has been configured.

    Created once, on first start. An operator who disables or retimes it keeps that choice:
    this never rewrites a row that already exists.
    """
    try:
        async with db_session.session_scope() as session:
            if await session.get(BatchSchedule, "GB") is None:
                session.add(
                    BatchSchedule(
                        country="GB",
                        enabled=True,
                        weekday=MONDAY,
                        hour_utc=DEFAULT_HOUR_UTC,
                        minute_utc=0,
                    )
                )
    except Exception as exc:
        logger.warning(
            "the default GB schedule was not created: %s", db_session.describe_error(exc)
        )


async def _record(country: str, *, fired: bool, success: bool, reason: str) -> None:
    """Write down what happened on this firing, including a skip and why."""
    async with db_session.session_scope() as session:
        row = await session.get(BatchSchedule, country)
        if row is None:
            return
        now = dt.datetime.now(dt.UTC)
        if fired:
            row.last_fired_at = now
        if success:
            row.last_success_at = now
        row.last_reason = reason


async def tick(now: dt.datetime | None = None) -> list[dict[str, str]]:
    """One pass over the schedules. Returns what was done, which is what the tests read."""
    moment = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    outcomes: list[dict[str, str]] = []

    async with db_session.session_scope() as session:
        rows = list((await session.execute(select(BatchSchedule))).scalars().all())
        # Read the values out before leaving the session; firing does its own database work.
        candidates = [
            (row.country, due_at(row, moment), too_soon(row, moment), row.enabled) for row in rows
        ]

    for country, due, soon, enabled in candidates:
        if not enabled or not due:
            continue

        if soon:
            reason = (
                f"Skipped: fewer than {MIN_DAYS_BETWEEN_RUNS} days have passed since the last "
                "successful run."
            )
            await _record(country, fired=True, success=False, reason=reason)
            outcomes.append({"country": country, "outcome": "skipped", "reason": reason})
            continue

        existing = await store.running_for(country)
        if existing is not None:
            reason = (
                f"Skipped: the batch started {existing['created_at']} is still "
                f"{existing['status'].lower()}."
            )
            await _record(country, fired=True, success=False, reason=reason)
            outcomes.append({"country": country, "outcome": "skipped", "reason": reason})
            continue

        try:
            started = await runner.start(country, kind=store.SCHEDULED)
        except Exception as exc:
            reason = f"The scheduled batch could not start: {exc}"
            await _record(country, fired=True, success=False, reason=reason)
            outcomes.append({"country": country, "outcome": "failed", "reason": reason})
            logger.warning("scheduled batch for %s did not start: %s", country, exc)
            continue

        reason = f"Started batch {started['id']}."
        await _record(country, fired=True, success=True, reason=reason)
        outcomes.append({"country": country, "outcome": "started", "reason": reason})
        logger.info("scheduled batch started for %s: %s", country, started["id"])

    return outcomes


async def loop() -> None:
    """Wake every minute and fire whatever is due. Started with the application.

    The default schedule is created by the lifespan before the server accepts a request, not
    here: creating it from a background task would race a `PUT /api/batch/schedules` that
    arrives in the same moment.
    """
    while True:
        try:
            await asyncio.sleep(TICK_S)
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A scheduler that dies on one bad tick stops firing for good, so the loop
            # survives and the traceback is logged rather than swallowed.
            logger.exception("the batch scheduler tick failed")
