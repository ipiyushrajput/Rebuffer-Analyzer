"""Bulk analysis.

A file of channels is validated row by row, then analysed with a bounded concurrency and a
per-host connection limit so one CDN is never hammered. The output is a consolidated ranked
report plus a ZIP carrying every per-channel report.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import case, func, select

from app.api.schemas import JobOptionsIn
from app.bulk import parsers
from app.db import session as db_session
from app.db.models import BulkItem
from app.db.models import Job as JobRow
from app.db.paging import newest_rows
from app.jobs.manager import JobHandle, job_manager
from app.reports import service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bulk", tags=["bulk"])

SNAPSHOT_DURATION_S = 180.0  # 3 minutes, inside the 2-5 minute snapshot window.
MAX_UPLOAD_BYTES = 16 * 1024 * 1024

_bulk_tasks: dict[str, asyncio.Task[None]] = {}


@router.get("/template.{fmt}")
async def template(fmt: str) -> Response:
    if fmt == "csv":
        return Response(
            content=parsers.template_csv(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="rba-bulk-template.csv"'},
        )
    if fmt == "json":
        return Response(
            content=parsers.template_json(),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="rba-bulk-template.json"'},
        )
    if fmt == "xlsx":
        return Response(
            content=parsers.template_xlsx(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="rba-bulk-template.xlsx"'},
        )
    raise HTTPException(status_code=404, detail="Template format must be csv, xlsx or json")


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"The file is larger than {MAX_UPLOAD_BYTES // 1024 // 1024} MB",
        )
    return data


@router.post("/validate")
async def validate(file: UploadFile = File(...)) -> dict[str, Any]:
    """Parse and validate without starting anything, so errors are corrected first."""
    data = await _read_upload(file)
    try:
        rows, mapping = parsers.parse(data, file.filename or "upload.csv")
    except parsers.BulkParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "rows": [row.as_dict() for row in rows],
        "errors": [row.as_dict() for row in rows if not row.valid],
        "column_mapping": mapping,
        "valid_count": sum(1 for row in rows if row.valid),
        "total": len(rows),
    }


@router.post("/jobs", status_code=201)
async def create_job(
    file: UploadFile = File(...),
    mode: str = Form(default="snapshot"),
    duration_minutes: int | None = Form(default=None),
    concurrency: int = Form(default=5),
    options: str | None = Form(default=None),
) -> dict[str, Any]:
    if mode not in ("snapshot", "aging"):
        raise HTTPException(status_code=400, detail="Mode must be snapshot or aging")
    if mode == "aging" and not duration_minutes:
        raise HTTPException(status_code=400, detail="Aging mode requires duration_minutes")

    data = await _read_upload(file)
    try:
        rows, mapping = parsers.parse(data, file.filename or "upload.csv")
    except parsers.BulkParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    valid = [row for row in rows if row.valid]
    if not valid:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "No row in the file is valid",
                "errors": [row.as_dict() for row in rows if not row.valid],
            },
        )

    try:
        parsed_options = JobOptionsIn(**json.loads(options)) if options else JobOptionsIn()
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"options is not valid: {exc}") from exc

    duration_s = (duration_minutes or 0) * 60 if mode == "aging" else SNAPSHOT_DURATION_S
    session_options = parsed_options.to_session_options(
        duration_s=duration_s, default_record=mode == "aging"
    )

    parent = JobHandle(
        id=uuid.uuid4().hex,
        type="bulk",
        channel_name=f"Bulk — {len(valid)} channel(s)",
        urls={"playback_url": None, "origin_url": None, "cdn_url": None, "ssai_url": None},
        options=session_options,
    )
    parent.status = "RUNNING"
    parent.started_at = dt.datetime.now(dt.UTC)
    parent.extra = {
        "mode": mode,
        "concurrency": concurrency,
        "total": len(valid),
        "invalid": len(rows) - len(valid),
        "column_mapping": mapping,
        "filename": file.filename,
    }
    job_manager.jobs[parent.id] = parent
    await job_manager._persist_job(parent)

    async with db_session.session_scope() as db:
        for row in valid:
            db.add(
                BulkItem(
                    bulk_job_id=parent.id,
                    row_index=row.row_index,
                    channel_name=row.channel_name,
                    playback_url=row.playback_url,
                    origin_url=row.origin_url,
                    cdn_url=row.cdn_url,
                    ssai_url=row.ssai_url,
                    extra={
                        "channel_id": row.channel_id,
                        "country": row.country,
                        "content_provider": row.content_provider,
                        "cdn": row.cdn,
                    },
                    status="PENDING",
                )
            )

    _bulk_tasks[parent.id] = asyncio.create_task(
        _run_bulk(parent, valid, session_options, concurrency),
        name=f"rba:bulk:{parent.id}",
    )

    return {
        **parent.summary(),
        "invalid_rows": [row.as_dict() for row in rows if not row.valid],
        "ws_url": f"/ws/jobs/{parent.id}",
    }


async def _run_bulk(
    parent: JobHandle,
    rows: list[parsers.BulkRow],
    options: Any,
    concurrency: int,
) -> None:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    completed = 0

    async def analyse(row: parsers.BulkRow) -> None:
        nonlocal completed
        async with semaphore:
            child = await job_manager.submit(
                job_type="bulk",
                playback_url=row.playback_url,
                origin_url=row.origin_url,
                cdn_url=row.cdn_url,
                ssai_url=row.ssai_url,
                channel_name=row.channel_name,
                options=options,
                parent_job_id=parent.id,
            )
            child.extra = {
                "channel_id": row.channel_id,
                "country": row.country,
                "row_index": row.row_index,
            }
            try:
                if child.task is not None:
                    await child.task
            except Exception as exc:
                logger.warning("bulk channel %s failed: %s", row.channel_name, type(exc).__name__)
            finally:
                completed += 1
                parent.extra["completed"] = completed
                await _update_item(parent.id, row.row_index, child)

    try:
        await asyncio.gather(*(analyse(row) for row in rows))
        parent.status = "COMPLETED"
    except asyncio.CancelledError:
        parent.status = "CANCELLED"
        raise
    except Exception as exc:
        parent.status = "FAILED"
        parent.error = f"{type(exc).__name__}: {exc}"
    finally:
        parent.finished_at = dt.datetime.now(dt.UTC)
        await job_manager._persist_job(parent)
        try:
            children = job_manager.children(parent.id)
            summary = await _build_rows(parent.id, children)
            report = await service.generate_bulk(parent, children, rows=summary)
            parent.extra["report"] = report
        except Exception as exc:
            logger.warning("bulk report failed for %s: %s", parent.id, type(exc).__name__)


async def _update_item(bulk_job_id: str, row_index: int, child: JobHandle) -> None:
    verdict = child.result.verdict if child.result else None
    async with db_session.session_scope() as db:
        rows = (
            (
                await db.execute(
                    select(BulkItem).where(
                        BulkItem.bulk_job_id == bulk_job_id, BulkItem.row_index == row_index
                    )
                )
            )
            .scalars()
            .all()
        )
        for item in rows:
            item.status = child.status
            item.child_job_id = child.id
            item.error = child.error
            if verdict is not None:
                item.verdict_status = verdict.status.value
                item.owner = verdict.owner
                item.risk_score = float(verdict.risk_score)


async def _build_rows(bulk_job_id: str, children: list[JobHandle]) -> list[dict[str, Any]]:
    by_id = {child.id: child for child in children}
    rows: list[dict[str, Any]] = []
    async with db_session.session_scope() as db:
        items = (
            (
                await db.execute(
                    select(BulkItem)
                    .where(BulkItem.bulk_job_id == bulk_job_id)
                    .order_by(BulkItem.row_index)
                )
            )
            .scalars()
            .all()
        )
        for item in items:
            child = by_id.get(item.child_job_id or "")
            verdict = child.result.verdict if child and child.result else None
            worst_ratio = verdict.measured_rebuffer_ratio if verdict else None
            rows.append(
                {
                    "row_index": item.row_index,
                    "channel_name": item.channel_name,
                    "playback_url": item.playback_url,
                    "status": item.status,
                    "verdict_status": item.verdict_status,
                    "owner": item.owner,
                    "owner_label": verdict.owner_label if verdict else None,
                    "risk_score": item.risk_score,
                    "worst_ratio": worst_ratio,
                    "incident_count": verdict.incident_count if verdict else None,
                    "headline": verdict.headline if verdict else None,
                    "error": item.error,
                    "child_job_id": item.child_job_id,
                    "report_path": None,
                }
            )
    return rows


async def _stored_batches(limit: int) -> list[dict[str, Any]]:
    """Batches read back from the jobs table, newest first.

    A batch outlives the tab that started it: the operator refreshes the page, or the backend
    restarts, and the run carries on. The job manager only knows what this process started,
    so the listing reads the database as well — otherwise a running batch disappears from the
    screen while it is still working, which is what a refresh looked like.
    """
    async with db_session.session_scope() as db:
        # Sorted on the primary key alone; see `app/db/paging.py`.
        rows = await newest_rows(
            db,
            JobRow,
            where=(JobRow.type == "bulk", JobRow.parent_job_id.is_(None)),
            order_by=(JobRow.created_at.desc(),),
            limit=limit,
        )
        ids = [row.id for row in rows]
        # How far each batch got, counted from its own item rows rather than from a counter
        # held in memory: the items are what the run actually wrote down.
        counted: dict[str, tuple[int, int]] = {}
        if ids:
            for job_id, total, done in await db.execute(
                select(
                    BulkItem.bulk_job_id,
                    func.count(BulkItem.id),
                    func.sum(
                        case(
                            (BulkItem.status.in_(("COMPLETED", "FAILED", "CANCELLED")), 1),
                            else_=0,
                        )
                    ),
                )
                .where(BulkItem.bulk_job_id.in_(ids))
                .group_by(BulkItem.bulk_job_id)
            ):
                counted[str(job_id)] = (int(total or 0), int(done or 0))

        return [
            {
                "id": row.id,
                "type": row.type,
                "status": row.status,
                "channel_name": row.channel_name or "",
                "urls": {},
                "options": {},
                "progress": row.progress,
                "elapsed_s": 0.0,
                "remaining_s": 0.0,
                "created_at": row.created_at.isoformat(),
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "ends_at": row.ends_at.isoformat() if row.ends_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "counts": {},
                "verdict": row.verdict,
                "error": row.error,
                "parent_job_id": None,
                "total": counted.get(row.id, (0, 0))[0],
                "completed": counted.get(row.id, (0, 0))[1],
            }
            for row in rows
        ]


@router.get("/jobs")
async def list_jobs(limit: int = 25) -> dict[str, Any]:
    """Every batch this deployment knows about, the live ones first-hand.

    A batch still running in this process is reported from the job manager, which knows its
    progress; one that finished, or that a restart left behind, is read from the database.
    """
    live = {
        handle.id: handle.summary()
        for handle in job_manager.list_jobs(job_type="bulk")
        if handle.parent_job_id is None
    }
    try:
        stored = await _stored_batches(limit)
    except Exception as exc:
        logger.warning("stored bulk jobs could not be listed: %s", db_session.describe_error(exc))
        stored = []

    jobs = [live.get(row["id"], row) for row in stored]
    seen = {row["id"] for row in stored}
    # A batch started moments ago may not have reached the database yet.
    jobs = [summary for job_id, summary in live.items() if job_id not in seen] + jobs
    return {"jobs": jobs[:limit], "count": len(jobs)}


@router.get("/jobs/{job_id}")
async def read_job(job_id: str) -> dict[str, Any]:
    parent = job_manager.get(job_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="Bulk job not found")
    children = job_manager.children(job_id)
    rows = await _build_rows(job_id, children)
    done = sum(1 for row in rows if row["status"] in ("COMPLETED", "CANCELLED", "FAILED"))
    return {
        **parent.summary(),
        "items": rows,
        "completed": done,
        "total": len(rows),
        "progress": done / len(rows) if rows else 0.0,
    }


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str) -> dict[str, Any]:
    parent = job_manager.get(job_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="Bulk job not found")
    for child in job_manager.children(job_id):
        await job_manager.cancel(child.id)
    task = _bulk_tasks.get(job_id)
    if task is not None and not task.done():
        task.cancel()
    parent.status = "CANCELLED"
    parent.finished_at = dt.datetime.now(dt.UTC)
    return parent.summary()


async def _bulk_report(job_id: str) -> dict[str, Any]:
    """The bulk job's stored report record, generated on first request."""
    parent = job_manager.get(job_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="Bulk job not found")
    report = parent.extra.get("report")
    if not report:
        children = job_manager.children(job_id)
        rows = await _build_rows(job_id, children)
        report = await service.generate_bulk(parent, children, rows=rows)
        parent.extra["report"] = report
    return dict(report)


@router.get("/jobs/{job_id}/report.{fmt}")
async def download_consolidated(job_id: str, fmt: str) -> Response:
    """The consolidated report, served from the database."""
    report = await _bulk_report(job_id)
    stored = await service.get_report(int(report["id"]))
    if stored is None:
        raise HTTPException(status_code=404, detail="The consolidated report is not stored")
    data, media, filename = stored

    if fmt == "html":
        return Response(
            content=data,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    if fmt == "pdf":
        from app.reports.render_pdf import PdfUnavailable, render_pdf_bytes

        try:
            pdf = await render_pdf_bytes(data.decode("utf-8"))
        except PdfUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="bulk-{job_id[:8]}.pdf"'},
        )
    raise HTTPException(status_code=400, detail="Format must be html or pdf")


@router.get("/jobs/{job_id}/reports.zip")
async def download_bundle(job_id: str) -> Response:
    """Every per-channel report plus the consolidated one, as a ZIP from the database."""
    report = await _bulk_report(job_id)
    stored = await service.get_report(int(report["zip_id"]))
    if stored is None:
        raise HTTPException(status_code=404, detail="The report archive is not stored")
    data, _media, _filename = stored
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="bulk-{job_id[:8]}.zip"'},
    )
