"""A run's folder in object storage, fixed once and read back from the job row."""

from __future__ import annotations

from app.db import session as db_session
from app.db.models import Job as JobRow
from app.storage import keys


async def prefix_for(job_id: str) -> str:
    """The run's storage folder, created on first use and kept on the job row.

    It is computed once — channel name, playback URL and start date — and stored, so a channel
    renamed later, or a date that crosses midnight, still finds the files of this run. A report
    that belongs to no job (a batch's summary) is filed under `batches/`.
    """
    async with db_session.session_scope() as session:
        row = await session.get(JobRow, job_id)
        if row is None:
            return f"batches/{keys.slug(job_id, 64)}"
        if row.storage_prefix:
            return row.storage_prefix
        prefix = keys.job_prefix(
            job_id=row.id,
            channel_name=row.channel_name,
            playback_url=row.playback_url,
            created_at=row.created_at,
        )
        row.storage_prefix = prefix
        return prefix


async def known_prefix(job_id: str) -> str | None:
    """The run's folder only if one was ever written; a lookup that creates nothing."""
    async with db_session.session_scope() as session:
        row = await session.get(JobRow, job_id)
        return row.storage_prefix if row is not None else None
