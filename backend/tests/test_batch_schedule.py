"""The weekly firing: when it runs, and the two cases where it must not.

A schedule that fires twice in a week analyses the same channels twice; one that fires while
the previous batch is still running has two batches scanning one country. Both are refused
here, and — just as importantly — both are written down, because a schedule that skips
silently cannot be told apart from one that has stopped working.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.batch import scheduler, store
from app.db import session as db_session
from app.db.models import Batch, BatchSchedule
from app.main import create_app

# Monday 02:00 UTC, the default the GB schedule is created with.
MONDAY_0200 = dt.datetime(2026, 9, 14, 2, 0, tzinfo=dt.UTC)


def row(**patch: object) -> BatchSchedule:
    base = {
        "country": "GB",
        "enabled": True,
        "weekday": scheduler.MONDAY,
        "hour_utc": 2,
        "minute_utc": 0,
    }
    return BatchSchedule(**{**base, **patch})


# -- when a schedule is due --------------------------------------------------


def test_a_schedule_is_due_at_its_time_on_its_weekday() -> None:
    assert scheduler.due_at(row(), MONDAY_0200) is True


def test_a_schedule_is_not_due_before_its_time() -> None:
    assert scheduler.due_at(row(), MONDAY_0200 - dt.timedelta(minutes=1)) is False


def test_a_schedule_is_not_due_on_another_day() -> None:
    """Tuesday at exactly the right time is still the wrong day."""
    assert scheduler.due_at(row(), MONDAY_0200 + dt.timedelta(days=1)) is False


def test_a_tick_that_lands_late_still_fires_inside_the_grace_window() -> None:
    """
    A restart, or a slow tick, must not cost a whole week.

    The check is "has its time passed, recently", not "is it this exact minute".
    """
    late = MONDAY_0200 + dt.timedelta(minutes=scheduler.GRACE_MINUTES - 1)
    assert scheduler.due_at(row(), late) is True


def test_a_tick_long_past_the_window_does_not_fire() -> None:
    """Firing at 09:00 for an 02:00 schedule would be a surprise, not a recovery."""
    very_late = MONDAY_0200 + dt.timedelta(minutes=scheduler.GRACE_MINUTES + 1)
    assert scheduler.due_at(row(), very_late) is False


def test_a_disabled_schedule_is_never_due() -> None:
    assert scheduler.due_at(row(enabled=False), MONDAY_0200) is False


# -- the seven-day gap -------------------------------------------------------


def test_a_run_inside_seven_days_of_the_last_success_is_too_soon() -> None:
    six_days_ago = MONDAY_0200 - dt.timedelta(days=6)
    assert scheduler.too_soon(row(last_success_at=six_days_ago), MONDAY_0200) is True


def test_a_run_seven_days_after_the_last_success_is_allowed() -> None:
    a_week_ago = MONDAY_0200 - dt.timedelta(days=7)
    assert scheduler.too_soon(row(last_success_at=a_week_ago), MONDAY_0200) is False


def test_a_country_that_has_never_run_is_never_too_soon() -> None:
    assert scheduler.too_soon(row(last_success_at=None), MONDAY_0200) is False


def test_the_gap_is_measured_from_the_last_success_not_the_last_firing() -> None:
    """
    A firing that skipped is not a run.

    Measuring from `last_fired_at` would let one skip push the next real run out by a week.
    """
    schedule = row(
        last_fired_at=MONDAY_0200 - dt.timedelta(hours=1),
        last_success_at=MONDAY_0200 - dt.timedelta(days=8),
    )
    assert scheduler.too_soon(schedule, MONDAY_0200) is False


def test_a_naive_stored_timestamp_is_read_as_utc() -> None:
    """SQLite returns datetimes without a zone; comparing them raw would raise."""
    naive = (MONDAY_0200 - dt.timedelta(days=3)).replace(tzinfo=None)
    assert scheduler.too_soon(row(last_success_at=naive), MONDAY_0200) is True


# -- a tick against the database ---------------------------------------------


@pytest.fixture()
def api() -> TestClient:
    with TestClient(create_app()) as client:
        yield client


async def _clear() -> None:
    from sqlalchemy import delete

    async with db_session.session_scope() as session:
        await session.execute(delete(BatchSchedule))
        await session.execute(delete(Batch))


@pytest.mark.asyncio()
async def test_the_default_gb_schedule_is_created_once_and_never_rewritten(api: TestClient) -> None:
    """An operator who retimes or disables it keeps that choice across restarts."""
    await _clear()
    await scheduler.ensure_default()

    schedules = await scheduler.list_schedules()
    gb = next(item for item in schedules if item["country"] == "GB")
    assert (gb["weekday"], gb["hour_utc"], gb["minute_utc"]) == (0, 2, 0)
    assert gb["enabled"] is True

    await scheduler.save_schedule(country="GB", enabled=False, weekday=3, hour_utc=9, minute_utc=30)
    await scheduler.ensure_default()

    again = next(item for item in await scheduler.list_schedules() if item["country"] == "GB")
    assert (again["enabled"], again["weekday"], again["hour_utc"]) == (False, 3, 9)


@pytest.mark.asyncio()
async def test_a_tick_inside_seven_days_skips_and_records_the_reason(api: TestClient) -> None:
    await _clear()
    await scheduler.save_schedule(country="GB", enabled=True, weekday=0, hour_utc=2, minute_utc=0)
    async with db_session.session_scope() as session:
        stored = await session.get(BatchSchedule, "GB")
        assert stored is not None
        stored.last_success_at = MONDAY_0200 - dt.timedelta(days=2)

    outcomes = await scheduler.tick(MONDAY_0200)

    assert outcomes == [
        {
            "country": "GB",
            "outcome": "skipped",
            "reason": "Skipped: fewer than 7 days have passed since the last successful run.",
        }
    ]
    # The skip is written to the schedule, so an operator can see it happened.
    gb = next(item for item in await scheduler.list_schedules() if item["country"] == "GB")
    assert gb["last_fired_at"] is not None
    assert "fewer than 7 days" in (gb["last_reason"] or "")


@pytest.mark.asyncio()
async def test_a_tick_while_a_batch_is_running_skips_and_names_it(api: TestClient) -> None:
    """Two batches for one country would scan the same channels twice."""
    await _clear()
    await scheduler.save_schedule(country="GB", enabled=True, weekday=0, hour_utc=2, minute_utc=0)
    await store.create(batch_id="already-running", country="GB", kind="manual", snapshot={})

    outcomes = await scheduler.tick(MONDAY_0200)

    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "skipped"
    assert "still queued" in outcomes[0]["reason"]

    gb = next(item for item in await scheduler.list_schedules() if item["country"] == "GB")
    assert gb["last_success_at"] is None, "a skip is not a successful run"


@pytest.mark.asyncio()
async def test_a_tick_on_the_wrong_day_does_nothing_at_all(api: TestClient) -> None:
    await _clear()
    await scheduler.save_schedule(country="GB", enabled=True, weekday=0, hour_utc=2, minute_utc=0)

    assert await scheduler.tick(MONDAY_0200 + dt.timedelta(days=2)) == []

    gb = next(item for item in await scheduler.list_schedules() if item["country"] == "GB")
    assert gb["last_fired_at"] is None


@pytest.mark.asyncio()
async def test_a_schedule_can_be_configured_for_any_country_and_removed(api: TestClient) -> None:
    """GB is the default, not a special case: the design carries more countries."""
    await _clear()
    saved = await scheduler.save_schedule(
        country="us", enabled=True, weekday=2, hour_utc=6, minute_utc=15
    )

    assert saved["country"] == "US"
    assert (saved["weekday"], saved["hour_utc"], saved["minute_utc"]) == (2, 6, 15)
    assert await scheduler.delete_schedule("US") is True
    assert await scheduler.delete_schedule("US") is False


def test_the_schedule_endpoints_read_and_write(api: TestClient) -> None:
    written = api.put(
        "/api/batch/schedules",
        json={"country": "GB", "enabled": True, "weekday": 0, "hour_utc": 2, "minute_utc": 0},
    )
    assert written.status_code == 200
    assert written.json()["country"] == "GB"

    listed = api.get("/api/batch/schedules").json()
    assert listed["min_days_between_runs"] == 7
    assert any(item["country"] == "GB" for item in listed["schedules"])


def test_a_schedule_for_a_country_nobody_serves_is_refused(api: TestClient) -> None:
    response = api.put("/api/batch/schedules", json={"country": "ZZ"})
    assert response.status_code == 400
    assert "not a supported country" in response.json()["detail"]


@pytest.mark.asyncio()
async def test_a_schedule_written_while_the_default_appears_updates_rather_than_failing(
    api: TestClient,
) -> None:
    """
    The read and the write are two statements, so the row can be created between them — by
    `ensure_default` on a host that has just started, by a second operator, or by a second
    analyzer instance against the same database. The second attempt updates the row that won.
    """
    await _clear()

    original = scheduler._write_schedule
    calls: list[int] = []

    async def racing(*args: object, **kwargs: object) -> dict[str, Any] | None:
        """The first attempt loses the race: GB is created under it before it writes."""
        if not calls:
            calls.append(1)
            await scheduler.ensure_default()
            return None
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    scheduler._write_schedule = racing  # type: ignore[assignment]
    try:
        saved = await scheduler.save_schedule(
            country="GB", enabled=False, weekday=4, hour_utc=11, minute_utc=45
        )
    finally:
        scheduler._write_schedule = original  # type: ignore[assignment]

    assert calls == [1], "the first attempt ran and lost"
    assert (saved["weekday"], saved["hour_utc"], saved["minute_utc"]) == (4, 11, 45)
    assert saved["enabled"] is False, "the caller's values won, not the default's"

    stored = next(item for item in await scheduler.list_schedules() if item["country"] == "GB")
    assert (stored["weekday"], stored["hour_utc"], stored["minute_utc"]) == (4, 11, 45)


def test_the_schedule_endpoints_write_over_the_default_the_host_created(
    api: TestClient,
) -> None:
    """The host creates GB on start, so the first PUT for GB is an update, not an insert."""
    written = api.put(
        "/api/batch/schedules",
        json={"country": "GB", "enabled": True, "weekday": 3, "hour_utc": 5, "minute_utc": 30},
    )
    assert written.status_code == 200, written.text
    assert written.json()["weekday"] == 3

    listed = api.get("/api/batch/schedules").json()["schedules"]
    assert [item for item in listed if item["country"] == "GB"] != []
    assert len([item for item in listed if item["country"] == "GB"]) == 1
