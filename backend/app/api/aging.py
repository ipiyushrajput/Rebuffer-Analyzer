"""Aging jobs.

An aging job runs server-side for a user-chosen duration and survives the browser closing
and a backend restart. Cancelling it still produces a report from the data collected so far.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select

from app.api import job_samples
from app.api.job_views import row_summary as _row_summary
from app.api.job_views import stored_result
from app.api.schemas import AGING_PRESETS_MINUTES, AgingJobIn
from app.db import session as db_session
from app.db.models import Job as JobRow
from app.jobs.manager import job_manager
from app.reports import service

router = APIRouter(prefix="/aging", tags=["aging"])


@router.get("/presets")
async def presets() -> dict[str, Any]:
    return {
        "presets_minutes": list(AGING_PRESETS_MINUTES),
        "custom_range_minutes": {"min": 1, "max": 1440},
    }


@router.post("/jobs", status_code=201)
async def create_job(payload: AgingJobIn) -> dict[str, Any]:
    options = payload.options.to_session_options(
        duration_s=payload.duration_minutes * 60, default_record=True
    )
    handle = await job_manager.submit(
        job_type="aging",
        playback_url=payload.playback_url,
        origin_url=payload.origin_url,
        cdn_url=payload.cdn_url,
        ssai_url=payload.ssai_url,
        channel_name=payload.channel_name,
        options=options,
    )
    return {**handle.summary(), "ws_url": f"/ws/jobs/{handle.id}"}


@router.get("/jobs")
async def list_jobs(include_finished: bool = Query(default=True)) -> dict[str, Any]:
    live = {h.id: h.summary() for h in job_manager.list_jobs(job_type="aging")}

    # Jobs from an earlier process run are read back from the database.
    try:
        async with db_session.session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(JobRow)
                        .where(JobRow.type == "aging")
                        .order_by(JobRow.created_at.desc())
                        .limit(200)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                if row.id in live:
                    continue
                if not include_finished and row.status in ("COMPLETED", "CANCELLED", "FAILED"):
                    continue
                live[row.id] = _row_summary(row)
    except Exception:
        pass

    jobs = sorted(live.values(), key=lambda item: item["created_at"], reverse=True)
    return {"jobs": jobs, "count": len(jobs)}


@router.get("/jobs/{job_id}")
async def read_job(job_id: str) -> dict[str, Any]:
    handle = job_manager.get(job_id)
    if handle is not None:
        return {**handle.summary(), "ws_url": f"/ws/jobs/{job_id}"}
    async with db_session.session_scope() as session:
        row = await session.get(JobRow, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return _row_summary(row)


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str) -> dict[str, Any]:
    handle = await job_manager.cancel(job_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return handle.summary()


@router.get("/jobs/{job_id}/result")
async def read_result(job_id: str) -> dict[str, Any]:
    """The finished result, or the stored findings and incidents for a job from an earlier run."""
    handle = job_manager.get(job_id)
    if handle is not None and handle.result is not None:
        return handle.result.as_dict()

    async with db_session.session_scope() as session:
        row = await session.get(JobRow, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return await stored_result(session, row)


@router.get("/jobs/{job_id}/samples")
async def read_samples(
    job_id: str,
    start: str | None = Query(default=None, alias="from"),
    end: str | None = Query(default=None, alias="to"),
    max_points: int = Query(default=job_samples.DEFAULT_MAX_POINTS, ge=50, le=20000),
) -> dict[str, Any]:
    """The stored samples for one job, shaped for the charts and thinned for drawing.

    An aging run has no socket anyone is watching, so its charts are drawn from what was
    written down. The raw rows stay in the database; what comes back here is what renders.
    """

    def moment(raw: str | None) -> dt.datetime | None:
        if not raw:
            return None
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"{raw!r} is not an ISO 8601 timestamp"
            ) from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)

    async with db_session.session_scope() as session:
        row = await session.get(JobRow, job_id)
        if row is None and job_manager.get(job_id) is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return await job_samples.read_samples(
            session, job_id, start=moment(start), end=moment(end), max_points=max_points
        )


@router.post("/jobs/{job_id}/report")
async def create_report(job_id: str, format: str = Query(default="html")) -> dict[str, Any]:
    handle = job_manager.get(job_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if handle.result is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "This job has produced no result yet. Cancel it to report on what was collected."
            ),
        )
    return await service.generate(handle, fmt=format)


@router.get("/jobs/{job_id}/report.{fmt}")
async def download_report(job_id: str, fmt: str) -> Response:
    """Render the report for this job and serve it from the database."""
    if fmt not in ("html", "pdf"):
        raise HTTPException(status_code=400, detail="Format must be html or pdf")
    handle = job_manager.get(job_id)
    if handle is None or handle.result is None:
        raise HTTPException(status_code=404, detail="No finished result for this job")

    generated = await service.generate(handle, fmt=fmt)
    stored = await service.get_report(int(generated["id"]))
    if stored is None:
        raise HTTPException(status_code=404, detail="The report is not stored")
    data, media, filename = stored
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
