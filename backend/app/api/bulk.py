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
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import select

from app.api.schemas import JobOptionsIn
from app.bulk import parsers
from app.db import session as db_session
from app.db.models import BulkItem
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


@router.get("/jobs")
async def list_jobs() -> dict[str, Any]:
    jobs = [h.summary() for h in job_manager.list_jobs(job_type="bulk") if h.parent_job_id is None]
    return {"jobs": jobs, "count": len(jobs)}


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


@router.get("/jobs/{job_id}/report.{fmt}")
async def download_consolidated(job_id: str, fmt: str) -> FileResponse:
    parent = job_manager.get(job_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="Bulk job not found")
    report = parent.extra.get("report")
    if not report:
        children = job_manager.children(job_id)
        rows = await _build_rows(job_id, children)
        report = await service.generate_bulk(parent, children, rows=rows)
        parent.extra["report"] = report

    if fmt == "html":
        path = Path(report["consolidated"])
        return FileResponse(path=path, media_type="text/html; charset=utf-8", filename=path.name)
    if fmt == "pdf":
        from app.reports.render_pdf import PdfUnavailable, render_pdf

        html = Path(report["consolidated"]).read_text(encoding="utf-8")
        pdf_path = Path(report["consolidated"]).with_suffix(".pdf")
        try:
            await render_pdf(html, pdf_path)
        except PdfUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return FileResponse(path=pdf_path, media_type="application/pdf", filename=pdf_path.name)
    raise HTTPException(status_code=400, detail="Format must be html or pdf")


@router.get("/jobs/{job_id}/reports.zip")
async def download_bundle(job_id: str) -> FileResponse:
    parent = job_manager.get(job_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="Bulk job not found")
    report = parent.extra.get("report")
    if not report:
        children = job_manager.children(job_id)
        rows = await _build_rows(job_id, children)
        report = await service.generate_bulk(parent, children, rows=rows)
        parent.extra["report"] = report
    path = Path(report["zip"])
    return FileResponse(path=path, media_type="application/zip", filename=path.name)
