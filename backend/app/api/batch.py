"""The Automated Batch API.

A batch is backend work. Everything here reads or writes rows, and nothing depends on the
browser that started a run still being open: the listing, the progress, the log and the report
all come from the database, so a reload, a different browser or a restarted process all see
the same batch.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.batch import exports, reporting, runner, scheduler, store
from app.batch import settings as batch_settings
from app.batch.settings import BatchSettings
from app.cascada.service import CascadaScanError
from app.tvplus import catalogue as cat

router = APIRouter(prefix="/batch", tags=["batch"])


def _attachment(data: bytes, media: str, filename: str) -> Response:
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -- settings ----------------------------------------------------------------


@router.get("/settings")
async def read_settings() -> dict[str, Any]:
    """The batch settings, and the defaults they are compared against in the UI."""
    current = await batch_settings.load()
    return {
        "settings": current.model_dump(),
        "defaults": batch_settings.DEFAULTS.model_dump(),
        # The threshold and window live with the other analysis thresholds; echoed here so
        # the batch panel can state what a batch would use without holding a second copy.
        "effective": current.snapshot(),
    }


@router.put("/settings")
async def write_settings(body: BatchSettings) -> dict[str, Any]:
    """Save the batch settings. A batch already running keeps the snapshot it started with."""
    await batch_settings.save(body)
    return await read_settings()


# -- schedules ---------------------------------------------------------------


class ScheduleIn(BaseModel):
    country: str = Field(..., min_length=2, max_length=2)
    enabled: bool = True
    weekday: int = Field(default=0, ge=0, le=6)
    hour_utc: int = Field(default=2, ge=0, le=23)
    minute_utc: int = Field(default=0, ge=0, le=59)
    overrides: dict[str, Any] = Field(default_factory=dict)


@router.get("/schedules")
async def read_schedules() -> dict[str, Any]:
    return {
        "schedules": await scheduler.list_schedules(),
        "min_days_between_runs": scheduler.MIN_DAYS_BETWEEN_RUNS,
    }


@router.put("/schedules")
async def write_schedule(body: ScheduleIn) -> dict[str, Any]:
    try:
        code = cat.country(body.country).code
    except cat.CatalogueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await scheduler.save_schedule(
        country=code,
        enabled=body.enabled,
        weekday=body.weekday,
        hour_utc=body.hour_utc,
        minute_utc=body.minute_utc,
        overrides=body.overrides,
    )


@router.delete("/schedules/{country}")
async def remove_schedule(country: str) -> dict[str, Any]:
    removed = await scheduler.delete_schedule(country)
    if not removed:
        raise HTTPException(status_code=404, detail=f"No schedule is configured for {country}.")
    return {"deleted": True, "country": country.upper()}


# -- starting a batch --------------------------------------------------------


@router.get("/estimate")
async def estimate(country: str = Query(..., min_length=2, max_length=2)) -> dict[str, Any]:
    """What a batch for this country would involve, shown before it is started."""
    try:
        current = await batch_settings.load()
        return await runner.estimate(country, current)
    except cat.CatalogueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CascadaScanError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class BatchIn(BaseModel):
    country: str = Field(..., min_length=2, max_length=2)


@router.post("/batches", status_code=201)
async def create_batch(body: BatchIn) -> dict[str, Any]:
    """Start a batch. A country that already has one running is refused with that one's id."""
    try:
        return await runner.start(body.country, kind=store.MANUAL)
    except cat.CatalogueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except runner.BatchError as exc:
        existing = await store.running_for(body.country)
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "batch_id": existing["id"] if existing else None},
        ) from exc


@router.post("/batches/{batch_id}/rerun", status_code=201)
async def rerun_batch(batch_id: str) -> dict[str, Any]:
    """Run the same country again, with today's settings."""
    current = await store.read(batch_id)
    if current is None:
        raise HTTPException(status_code=404, detail="That batch does not exist.")
    try:
        return await runner.start(current["country"], kind=store.MANUAL)
    except runner.BatchError as exc:
        existing = await store.running_for(current["country"])
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "batch_id": existing["id"] if existing else None},
        ) from exc


# -- reading batches ---------------------------------------------------------


@router.get("/batches")
async def list_batches(
    country: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """Playground's list. Read from the database, so a restart loses nothing."""
    batches = await store.listing(country, limit)
    return {"batches": batches, "count": len(batches)}


@router.get("/batches/{batch_id}")
async def read_batch(batch_id: str) -> dict[str, Any]:
    """One batch with its channels: status, average, the analysis each one produced."""
    current = await store.read(batch_id, with_items=True)
    if current is None:
        raise HTTPException(status_code=404, detail="That batch does not exist.")
    return current


@router.delete("/batches/{batch_id}")
async def cancel_batch(batch_id: str) -> dict[str, Any]:
    """Stop after the channel in flight. Everything measured so far is kept."""
    current = await runner.cancel(batch_id)
    if current is None:
        raise HTTPException(status_code=404, detail="That batch does not exist.")
    return current


@router.get("/batches/{batch_id}/log")
async def read_log(batch_id: str, download: bool = Query(default=False)) -> Any:
    """Every step the batch took, to read on screen or to take away."""
    current = await store.read(batch_id)
    if current is None:
        raise HTTPException(status_code=404, detail="That batch does not exist.")
    lines = await store.log_lines(batch_id)
    if not download:
        return {"batch_id": batch_id, "lines": lines}

    text = "\n".join(f"{line['at']} {line['level']:<5} {line['message']}" for line in lines)
    return _attachment(
        text.encode("utf-8"),
        "text/plain",
        f"automated_batch_{current['country']}_{batch_id[:8]}.log",
    )


@router.get("/batches/{batch_id}/report.{fmt}")
async def download_report(batch_id: str, fmt: str) -> Response:
    """The report, rendered from the batch's stored rows."""
    if fmt not in ("csv", "xlsx"):
        raise HTTPException(status_code=400, detail="Format must be csv or xlsx")
    rendered = await reporting.render(batch_id, fmt)
    if rendered is None:
        raise HTTPException(status_code=404, detail="That batch does not exist.")
    data, media, name = rendered
    return _attachment(data, media, name)


@router.get("/columns")
async def report_columns() -> dict[str, Any]:
    """The report's column order, so the UI can state what a download contains."""
    current = await batch_settings.load()
    window_days = float(current.snapshot()["window_days"])
    return {
        "columns": list(exports.headings(window_days)),
        "window_days": window_days,
        "window_label": exports.window_label(window_days),
    }


# -- errors the runner raises asynchronously ---------------------------------


@router.get("/health")
async def batch_health() -> dict[str, Any]:
    """Whether a batch could run right now, and what would stop it."""
    from app.cascada.auth import resolve_auth

    auth = await resolve_auth()
    described = auth.describe()
    unfinished = await store.unfinished()
    return {
        "cascada_session": described.as_dict(),
        "ready": described.configured,
        "reason": ""
        if described.configured
        else "No CASCADA session is configured, so a batch cannot scan.",
        "running": [batch["id"] for batch in unfinished],
        "now_utc": dt.datetime.now(dt.UTC).isoformat(),
    }
