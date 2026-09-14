"""Analysed channels.

Every analysis that has finished — stopped by the operator, run to its duration, or failed —
is kept here with its verdict, its findings and the reports generated from it. A realtime
session is cleared from the Realtime tab the moment it stops, so this is where the operator
comes back to it.

Reads come from the database rather than the job manager, so a channel analysed before the
last restart is still listed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import delete, select

from app.api.job_views import TERMINAL, row_summary, stored_result
from app.db import session as db_session
from app.db.models import Finding as FindingRow
from app.db.models import Incident as IncidentRow
from app.db.models import Job as JobRow
from app.db.models import (
    PlayerSample,
    PlaylistSample,
    PlaylistSnapshot,
    Report,
    SegmentSample,
    VirtualBufferSample,
)
from app.jobs.manager import job_manager

router = APIRouter(prefix="/channels", tags=["channels"])

# Sample tables keyed by job_id. A deleted channel takes its measurements with it.
SAMPLE_TABLES = (
    PlaylistSample,
    SegmentSample,
    PlayerSample,
    VirtualBufferSample,
    PlaylistSnapshot,
    FindingRow,
    IncidentRow,
)


@router.get("")
async def list_channels(limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
    """Every finished analysis, newest first, with the reports generated from it."""
    async with db_session.session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(JobRow)
                    # Bulk children belong to their batch, not to this list.
                    .where(
                        JobRow.type.in_(("realtime", "aging")),
                        JobRow.status.in_(TERMINAL),
                        JobRow.parent_job_id.is_(None),
                    )
                    .order_by(JobRow.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        job_ids = [row.id for row in rows]

        reports_by_job: dict[str, list[dict[str, Any]]] = {}
        if job_ids:
            report_rows = (
                (
                    await session.execute(
                        select(Report)
                        .where(Report.job_id.in_(job_ids))
                        .order_by(Report.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            for report in report_rows:
                reports_by_job.setdefault(report.job_id, []).append(
                    {
                        "id": report.id,
                        "format": report.format,
                        "size_bytes": report.size_bytes,
                        "created_at": report.created_at.isoformat(),
                        "url": f"/api/reports/{report.id}/download",
                        "exists": report.size_bytes > 0,
                    }
                )

        channels = [{**row_summary(row), "reports": reports_by_job.get(row.id, [])} for row in rows]

    return {"channels": channels, "count": len(channels)}


@router.get("/{job_id}")
async def read_channel(job_id: str) -> dict[str, Any]:
    """One analysed channel: its summary, its stored result, and its reports."""
    async with db_session.session_scope() as session:
        row = await session.get(JobRow, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Analysed channel not found")
        result = await stored_result(session, row)
        reports = (
            (
                await session.execute(
                    select(Report).where(Report.job_id == job_id).order_by(Report.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        payload = {
            **row_summary(row),
            **result,
            "reports": [
                {
                    "id": report.id,
                    "format": report.format,
                    "size_bytes": report.size_bytes,
                    "created_at": report.created_at.isoformat(),
                    "url": f"/api/reports/{report.id}/download",
                    "exists": report.size_bytes > 0,
                }
                for report in reports
            ],
        }
    return payload


@router.delete("/{job_id}")
async def delete_channel(job_id: str) -> dict[str, Any]:
    """Delete one analysed channel: the job, its measurements and its report files."""
    handle = job_manager.get(job_id)
    if handle is not None and handle.status in ("RUNNING", "PENDING"):
        raise HTTPException(
            status_code=409,
            detail="This analysis is still running. Stop it before deleting it.",
        )

    removed_reports = 0
    removed_files = 0
    async with db_session.session_scope() as session:
        row = await session.get(JobRow, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Analysed channel not found")

        report_rows = (
            (await session.execute(select(Report).where(Report.job_id == job_id))).scalars().all()
        )
        removed_reports = len(report_rows)
        for report in report_rows:
            # The bytes go with the row. A disk mirror, when one was configured, goes too.
            if report.path:
                path = Path(report.path)
                if path.exists():
                    path.unlink(missing_ok=True)
                    removed_files += 1

        await session.execute(delete(Report).where(Report.job_id == job_id))
        for table in SAMPLE_TABLES:
            await session.execute(delete(table).where(table.job_id == job_id))
        await session.execute(delete(JobRow).where(JobRow.id == job_id))

    # The handle is dropped too, so a stopped session does not reappear in any listing.
    job_manager.jobs.pop(job_id, None)
    return {
        "deleted": True,
        "id": job_id,
        "reports_removed": removed_reports,
        "files_removed": removed_files,
    }
