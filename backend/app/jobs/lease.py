"""Job leases: several instances share one database and each job runs on exactly one of them.

A job is run by the instance holding its lease (`jobs.lease_owner`, `jobs.lease_expires_at`).
The owner renews every lease it holds every third of `RBA_JOB_LEASE_S`. An instance that stops
— a deploy, a crash, a host lost — stops renewing, its leases lapse, and another instance takes
the job over with `acquire`, which is one conditional UPDATE: of two instances racing for a
lapsed lease, exactly one row is updated and exactly one instance resumes the job.

A stop asked of any instance is written to `jobs.stop_requested`; the owner reads it on its next
renewal and stops the session, so the Stop button works whichever instance the browser reached.
An owner whose lease was taken over while it was stalled stops its own copy of the run rather
than writing the same job twice.
"""

from __future__ import annotations

import datetime as dt
import os
import socket
import uuid
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import or_, select, update

from app.config import get_settings
from app.db import session as db_session
from app.db.models import Job as JobRow


@lru_cache(maxsize=1)
def instance_id() -> str:
    """This process's name as a lease owner: configured, or host, pid and a random tail."""
    configured = get_settings().rba_instance_id.strip()
    if configured:
        return configured[:128]
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"[:128]


def lease_period() -> dt.timedelta:
    return dt.timedelta(seconds=max(15, get_settings().rba_job_lease_s))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def acquire(job_id: str) -> bool:
    """Take the lease of a job nobody holds, a lapsed one, or one this instance holds.

    A job with no row is not shared with anyone and counts as held.
    """
    now = _now()
    async with db_session.session_scope() as session:
        result = await session.execute(
            update(JobRow)
            .where(
                JobRow.id == job_id,
                or_(
                    JobRow.lease_owner.is_(None),
                    JobRow.lease_owner == instance_id(),
                    JobRow.lease_expires_at.is_(None),
                    JobRow.lease_expires_at < now,
                ),
            )
            .values(lease_owner=instance_id(), lease_expires_at=now + lease_period())
            .execution_options(synchronize_session=False)
        )
        if int(getattr(result, "rowcount", 0) or 0) == 1:
            return True
        exists = await session.scalar(select(JobRow.id).where(JobRow.id == job_id))
        return exists is None


@dataclass(slots=True)
class Renewal:
    """What the database said about the jobs this instance runs."""

    held: set[str]
    lost: set[str]
    stop_requested: set[str]


async def renew(job_progress: dict[str, float]) -> Renewal:
    """Extend every lease this instance holds, record progress, and read stop requests."""
    if not job_progress:
        return Renewal(held=set(), lost=set(), stop_requested=set())
    now = _now()
    expires = now + lease_period()
    held: set[str] = set()
    async with db_session.session_scope() as session:
        for job_id, progress in job_progress.items():
            result = await session.execute(
                update(JobRow)
                .where(JobRow.id == job_id, JobRow.lease_owner == instance_id())
                .values(lease_expires_at=expires, progress=progress)
                .execution_options(synchronize_session=False)
            )
            if int(getattr(result, "rowcount", 0) or 0) == 1:
                held.add(job_id)
        rows = (
            await session.execute(
                select(JobRow.id, JobRow.stop_requested, JobRow.lease_owner).where(
                    JobRow.id.in_(list(job_progress))
                )
            )
        ).all()
    stop = {row.id for row in rows if row.stop_requested and row.id in held}
    # A job another instance now owns is lost to this one: its lease lapsed while this
    # instance was stalled, and the other instance resumed it. A job that never had a row (an
    # unpersisted run) is not shared and is not lost.
    lost = {row.id for row in rows if row.lease_owner not in (None, instance_id())}
    return Renewal(held=held, lost=lost, stop_requested=stop)


async def release(job_id: str) -> None:
    async with db_session.session_scope() as session:
        await session.execute(
            update(JobRow)
            .where(JobRow.id == job_id, JobRow.lease_owner == instance_id())
            .values(lease_owner=None, lease_expires_at=None)
            .execution_options(synchronize_session=False)
        )


async def request_stop(job_id: str) -> bool:
    """Ask whichever instance runs this job to stop it. False when no such running job."""
    async with db_session.session_scope() as session:
        result = await session.execute(
            update(JobRow)
            .where(JobRow.id == job_id, JobRow.status.in_(("RUNNING", "PENDING")))
            .values(stop_requested=True)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1


async def owner_of(job_id: str) -> tuple[str | None, dt.datetime | None]:
    async with db_session.session_scope() as session:
        row = (
            await session.execute(
                select(JobRow.lease_owner, JobRow.lease_expires_at).where(JobRow.id == job_id)
            )
        ).first()
    if row is None:
        return None, None
    expires = row.lease_expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.UTC)
    return row.lease_owner, expires
