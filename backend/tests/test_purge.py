"""Deleting a finished aging job or bulk batch removes it and everything it wrote.

The aging and bulk lists used to offer only "cancel", which stops a run and leaves it listed
forever. `?purge=true` on their DELETE removes a finished run for good, through the same
`app/jobs/purge.py` the analysed-channels delete uses; a running one is refused.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.analysis.engine import SessionOptions
from app.db import session as db_session
from app.db.models import BulkItem, Job
from app.jobs import purge
from app.jobs.manager import JobHandle, job_manager
from app.main import create_app


@pytest.fixture(autouse=True)
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


async def _job(job_type: str, *, status: str = "COMPLETED", parent: str | None = None) -> str:
    job_id = uuid.uuid4().hex
    async with db_session.session_scope() as session:
        session.add(Job(id=job_id, type=job_type, status=status, parent_job_id=parent, params={}))
    return job_id


async def _exists(job_id: str) -> bool:
    async with db_session.session_scope() as session:
        return await session.get(Job, job_id) is not None


async def test_a_finished_aging_job_is_deleted_for_good() -> None:
    from app.api.aging import cancel_job

    job_id = await _job("aging")
    answer = await cancel_job(job_id, purge_record=True)

    assert answer["deleted"] is True and answer["jobs_removed"] == 1
    assert not await _exists(job_id)


async def test_a_running_aging_job_is_refused_rather_than_deleted() -> None:
    from app.api.aging import cancel_job

    job_id = await _job("aging", status="RUNNING")
    job_manager.jobs[job_id] = JobHandle(
        id=job_id, type="aging", channel_name="c", urls={}, options=SessionOptions()
    )
    job_manager.jobs[job_id].status = "RUNNING"
    try:
        with pytest.raises(HTTPException) as refused:
            await cancel_job(job_id, purge_record=True)
        assert refused.value.status_code == 409
        assert await _exists(job_id)
    finally:
        job_manager.jobs.pop(job_id, None)
        await purge.purge_job(job_id)


async def test_a_finished_batch_goes_with_every_run_and_row_in_it() -> None:
    from app.api.bulk import cancel_job

    batch = await _job("bulk")
    children = [await _job("realtime", parent=batch) for _ in range(2)]
    async with db_session.session_scope() as session:
        for index, child in enumerate(children):
            session.add(
                BulkItem(
                    bulk_job_id=batch,
                    row_index=index,
                    channel_name=f"ch{index}",
                    playback_url="https://cdn.example/m.m3u8",
                    status="COMPLETED",
                    child_job_id=child,
                )
            )

    answer = await cancel_job(batch, purge_record=True)

    assert answer["jobs_removed"] == 3
    for job_id in (batch, *children):
        assert not await _exists(job_id)
    async with db_session.session_scope() as session:
        left = await session.scalar(
            select(func.count(BulkItem.id)).where(BulkItem.bulk_job_id == batch)
        )
    assert left == 0


async def test_deleting_a_run_that_does_not_exist_says_so() -> None:
    from app.api.aging import cancel_job

    with pytest.raises(HTTPException) as missing:
        await cancel_job("no-such-job", purge_record=True)
    assert missing.value.status_code == 404
