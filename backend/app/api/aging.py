"""Aging jobs.

An aging job runs server-side for a user-chosen duration and survives the browser closing
and a backend restart. Cancelling it still produces a report from the data collected so far.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.api.schemas import AGING_PRESETS_MINUTES, AgingJobIn
from app.db import session as db_session
from app.db.models import Finding as FindingRow
from app.db.models import Incident as IncidentRow
from app.db.models import Job as JobRow
from app.db.models import VirtualBufferSample
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
    live = {h.id: h.summary() for h in job_manager.list(job_type="aging")}

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


def _row_summary(row: JobRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "type": row.type,
        "status": row.status,
        "channel_name": row.channel_name or "(unnamed channel)",
        "urls": {"playback_url": row.playback_url},
        "options": row.params or {},
        "progress": 1.0 if row.status in ("COMPLETED", "CANCELLED", "FAILED") else row.progress,
        "elapsed_s": (
            (row.finished_at - row.started_at).total_seconds()
            if row.finished_at and row.started_at
            else 0.0
        ),
        "remaining_s": 0.0,
        "created_at": row.created_at.isoformat(),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ends_at": row.ends_at.isoformat() if row.ends_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "counts": (row.verdict or {}).get("counts", {}),
        "verdict": row.verdict,
        "error": row.error,
        "parent_job_id": row.parent_job_id,
        "from_database": True,
    }


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

        findings = (
            (await session.execute(select(FindingRow).where(FindingRow.job_id == job_id)))
            .scalars()
            .all()
        )
        incidents = (
            (await session.execute(select(IncidentRow).where(IncidentRow.job_id == job_id)))
            .scalars()
            .all()
        )
        buffer_rows = (
            (
                await session.execute(
                    select(VirtualBufferSample)
                    .where(VirtualBufferSample.job_id == job_id)
                    .order_by(VirtualBufferSample.ts)
                    .limit(20000)
                )
            )
            .scalars()
            .all()
        )

    series: dict[str, list[dict[str, Any]]] = {}
    for sample in buffer_rows:
        series.setdefault(sample.variant, []).append(
            {"at": sample.ts.isoformat(), "level_s": sample.level_s, "state": sample.state}
        )

    return {
        "from_database": True,
        "verdict": row.verdict,
        "findings": [
            {
                "rule_id": f.rule_id,
                "title": f.title,
                "layer": f.layer,
                "stream_layer": f.stream_layer,
                "severity": f.severity,
                "owner": f.owner,
                "owner_label": f.owner,
                "variant": f.variant,
                "detail": f.detail,
                "root_cause": f.root_cause,
                "fix": f.fix,
                "rebuffer_impact": f.rebuffer_impact,
                "count": f.count,
                "first_seen": f.first_seen.isoformat(),
                "last_seen": f.last_seen.isoformat(),
                "evidence": f.evidence,
                "layer_presence": f.layer_presence or {},
                "reference": "",
            }
            for f in findings
        ],
        "incidents": [
            {
                "kind": i.kind,
                "variant": i.variant,
                "started_at": i.started_at.isoformat(),
                "ended_at": i.ended_at.isoformat() if i.ended_at else None,
                "duration_s": i.duration_s,
                "cause_rule_ids": i.cause_finding_ids,
                "cause_chain": i.cause_chain,
                "detail": i.detail,
            }
            for i in incidents
        ],
        "vpb": {variant: {"series": points} for variant, points in series.items()},
    }


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
async def download_report(job_id: str, fmt: str) -> Any:
    from fastapi.responses import FileResponse

    if fmt not in ("html", "pdf"):
        raise HTTPException(status_code=400, detail="Format must be html or pdf")
    handle = job_manager.get(job_id)
    if handle is None or handle.result is None:
        raise HTTPException(status_code=404, detail="No finished result for this job")

    generated = await service.generate(handle, fmt=fmt)
    from pathlib import Path

    path = Path(generated["path"])
    media = "application/pdf" if fmt == "pdf" else "text/html; charset=utf-8"
    return FileResponse(path=path, media_type=media, filename=path.name)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
