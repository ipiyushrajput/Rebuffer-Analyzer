"""Deleting a finished run, and everything it wrote, in one place.

Analysed channels, the aging job list and the bulk list all delete runs. They delete the same
things — the job row, its measurements, its poll log, its reports and its recording — so they
all go through here, and a table added to a run's footprint is added once.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select

from app.config import get_settings
from app.db import session as db_session
from app.db.models import (
    BulkItem,
    ManifestBody,
    ManifestPoll,
    PlayerSample,
    PlaylistSample,
    PlaylistSnapshot,
    Report,
    SegmentSample,
    VirtualBufferSample,
)
from app.db.models import Finding as FindingRow
from app.db.models import Incident as IncidentRow
from app.db.models import Job as JobRow
from app.jobs.manager import job_manager
from app.storage import get_storage

# Every table a run writes rows to, keyed by `job_id`.
SAMPLE_TABLES = (
    PlaylistSample,
    SegmentSample,
    PlayerSample,
    VirtualBufferSample,
    PlaylistSnapshot,
    FindingRow,
    IncidentRow,
    # The poll log goes with the run it recorded.
    ManifestPoll,
    ManifestBody,
)

RUNNING = ("RUNNING", "PENDING")


@dataclass
class Purged:
    jobs: int = 0
    reports: int = 0
    files: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "jobs_removed": self.jobs,
            "reports_removed": self.reports,
            "files_removed": self.files,
        }


def is_running(job_id: str) -> bool:
    handle = job_manager.get(job_id)
    return handle is not None and handle.status in RUNNING


async def purge_job(job_id: str) -> Purged | None:
    """Delete one run and everything it wrote. None when there is no such run.

    The caller refuses a running job first: deleting the rows under a run that is still
    writing them would leave it writing to nothing.
    """
    purged = Purged()
    stored: list[str] = []
    async with db_session.session_scope() as session:
        row = await session.get(JobRow, job_id)
        if row is None:
            return None
        prefix = row.storage_prefix
        reports = (await session.execute(select(Report).where(Report.job_id == job_id))).scalars()
        for report in reports.all():
            purged.reports += 1
            if report.storage_key:
                stored.append(report.storage_key)
            # The bytes go with the row. A disk mirror, when one was configured, goes too.
            if report.path:
                path = Path(report.path)
                if path.exists():
                    path.unlink(missing_ok=True)
                    purged.files += 1
        await session.execute(delete(Report).where(Report.job_id == job_id))
        for table in SAMPLE_TABLES:
            await session.execute(delete(table).where(table.job_id == job_id))
        await session.execute(delete(JobRow).where(JobRow.id == job_id))
        purged.jobs = 1

    # The handle is dropped too, so a stopped run does not reappear in any listing.
    job_manager.jobs.pop(job_id, None)
    # Its folder in object storage — reports, recording and archives — goes with it.
    storage = get_storage()
    if prefix:
        purged.files += await storage.delete_prefix(prefix)
    for key in stored:
        if not prefix or not key.startswith(prefix + "/"):
            await storage.delete(key)
            purged.files += 1
    # And the recording, which is the one thing a run keeps on disk.
    recording = get_settings().rba_recordings_dir / job_id
    if recording.is_dir():
        shutil.rmtree(recording, ignore_errors=True)
    return purged


async def purge_batch(job_id: str) -> Purged | None:
    """Delete a bulk batch: every channel run it started, its item rows and itself."""
    async with db_session.session_scope() as session:
        if await session.get(JobRow, job_id) is None and job_manager.get(job_id) is None:
            return None
        children = (
            (await session.execute(select(JobRow.id).where(JobRow.parent_job_id == job_id)))
            .scalars()
            .all()
        )
    total = Purged()
    for child in [*children, *(h.id for h in job_manager.children(job_id))]:
        done = await purge_job(child)
        if done is not None:
            total.jobs += done.jobs
            total.reports += done.reports
            total.files += done.files
    async with db_session.session_scope() as session:
        await session.execute(delete(BulkItem).where(BulkItem.bulk_job_id == job_id))
    own = await purge_job(job_id)
    job_manager.jobs.pop(job_id, None)
    if own is not None:
        total.jobs += own.jobs
        total.reports += own.reports
        total.files += own.files
    return total
