"""Several instances share one database, and every job runs on exactly one of them.

A second instance is played by writing its name into a job's lease, which is all another
process would do.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient

from app.db import session as db_session
from app.db.models import Finding, Job
from app.jobs import lease
from app.main import create_app
from tests.fixtures.server import FixtureServer, build_simple_channel

OTHER = "other-instance:1:abc"
# Every row a test writes, removed afterwards: a RUNNING row whose lease later lapses would be
# taken over by the next test's instance, which is exactly what production does with one.
CREATED: list[str] = []


@pytest.fixture(autouse=True)
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture(autouse=True)
async def remove_rows() -> AsyncIterator[None]:
    yield
    from sqlalchemy import delete

    from app.jobs.manager import job_manager

    for job_id in CREATED:
        handle = job_manager.get(job_id)
        if handle is not None and handle.status in ("RUNNING", "PENDING"):
            await job_manager.cancel(job_id)
        job_manager.jobs.pop(job_id, None)
    async with db_session.session_scope() as session:
        await session.execute(delete(Job).where(Job.id.in_(CREATED)))
    CREATED.clear()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _row(**values: object) -> str:
    job_id = uuid.uuid4().hex
    async with db_session.session_scope() as session:
        session.add(Job(id=job_id, type="aging", status="RUNNING", params={}, **values))
    CREATED.append(job_id)
    return job_id


async def _read(job_id: str) -> Job:
    async with db_session.session_scope() as session:
        row = await session.get(Job, job_id)
        assert row is not None
        return row


async def test_a_live_lease_is_kept_and_a_lapsed_one_is_taken_over() -> None:
    held = await _row(lease_owner=OTHER, lease_expires_at=_now() + dt.timedelta(minutes=5))
    lapsed = await _row(lease_owner=OTHER, lease_expires_at=_now() - dt.timedelta(seconds=5))
    free = await _row()

    assert await lease.acquire(held) is False
    assert await lease.acquire(lapsed) is True
    assert await lease.acquire(free) is True
    assert (await _read(lapsed)).lease_owner == lease.instance_id()
    # Not shared with anyone: a job with no row counts as held.
    assert await lease.acquire("no-row-" + uuid.uuid4().hex) is True


async def test_renewal_extends_records_progress_and_reports_stops_and_losses() -> None:
    mine = await _row()
    stopped = await _row()
    taken = await _row()
    for job_id in (mine, stopped, taken):
        assert await lease.acquire(job_id)
    await lease.request_stop(stopped)
    async with db_session.session_scope() as session:
        row = await session.get(Job, taken)
        assert row is not None
        row.lease_owner = OTHER

    renewal = await lease.renew({mine: 0.4, stopped: 0.5, taken: 0.6})

    assert renewal.held == {mine, stopped}
    assert renewal.stop_requested == {stopped}
    assert renewal.lost == {taken}
    assert (await _read(mine)).progress == pytest.approx(0.4)


async def _start_aging(origin: FixtureServer, duration_s: float = 30.0):  # type: ignore[no-untyped-def]
    from app.api.schemas import JobOptionsIn
    from app.jobs.manager import job_manager

    url = build_simple_channel(origin, variant_count=1, segment_count=4, segment_duration=2.0)
    await job_manager.start()
    handle = await job_manager.submit(
        job_type="aging",
        playback_url=url,
        channel_name="Leased channel",
        options=JobOptionsIn().to_session_options(duration_s=duration_s, default_record=False),
    )
    CREATED.append(handle.id)
    return handle


async def test_a_stop_asked_of_another_instance_reaches_the_owner(origin: FixtureServer) -> None:
    from app.jobs.manager import job_manager

    handle = await _start_aging(origin)
    assert (await _read(handle.id)).lease_owner == lease.instance_id()

    # The other instance's Stop button writes the request; this instance owns the run.
    assert await lease.request_stop(handle.id)
    await job_manager.renew_leases()
    assert handle.task is not None
    await handle.task

    row = await _read(handle.id)
    assert row.status == "CANCELLED"
    assert row.lease_owner is None, "a finished job releases its lease"


async def test_a_job_taken_over_by_another_instance_stops_here_without_writing(
    origin: FixtureServer,
) -> None:
    from sqlalchemy import func, select

    from app.jobs.manager import job_manager

    handle = await _start_aging(origin)
    async with db_session.session_scope() as session:
        row = await session.get(Job, handle.id)
        assert row is not None
        # This instance stalled past its lease; another resumed the job and runs it.
        row.lease_owner = OTHER
        row.status = "RUNNING"

    await job_manager.renew_leases()
    assert handle.task is not None
    await handle.task

    assert handle.lease_lost
    row = await _read(handle.id)
    assert row.status == "RUNNING" and row.lease_owner == OTHER, "the new owner's run untouched"
    async with db_session.session_scope() as session:
        findings = await session.scalar(
            select(func.count(Finding.id)).where(Finding.job_id == handle.id)
        )
    assert findings == 0


async def test_only_a_lapsed_job_is_resumed_by_another_instance(origin: FixtureServer) -> None:
    from app.jobs.manager import job_manager

    url = build_simple_channel(origin, variant_count=1, segment_count=4, segment_duration=2.0)
    ends = _now() + dt.timedelta(seconds=20)
    common = {"playback_url": url, "channel_name": "Orphan", "ends_at": ends}
    live = await _row(
        lease_owner=OTHER, lease_expires_at=_now() + dt.timedelta(minutes=5), **common
    )
    orphan = await _row(
        lease_owner=OTHER, lease_expires_at=_now() - dt.timedelta(seconds=1), **common
    )
    await job_manager.start()

    resumed = await job_manager.resume_persisted_jobs()

    try:
        assert resumed == 1
        assert job_manager.get(orphan) is not None
        assert job_manager.get(live) is None
        assert (await _read(orphan)).lease_owner == lease.instance_id()
    finally:
        handle = job_manager.get(orphan)
        if handle is not None:
            await job_manager.cancel(orphan)


async def test_a_scheduled_firing_is_taken_by_one_instance() -> None:
    from app.batch import scheduler
    from app.db.models import BatchSchedule

    country = "ZZ-" + uuid.uuid4().hex[:6]
    async with db_session.session_scope() as session:
        session.add(
            BatchSchedule(country=country, weekday=0, hour_utc=0, minute_utc=0, enabled=True)
        )
    moment = _now()

    assert await scheduler._claim(country, moment) is True
    assert await scheduler._claim(country, moment + dt.timedelta(seconds=1)) is False
