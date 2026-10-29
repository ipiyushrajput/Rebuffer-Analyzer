"""Feedback on findings and per-channel acknowledgements (see `app/analysis/review.py`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.analysis import review
from app.db import session as db_session
from app.db.models import Job as JobRow

router = APIRouter(tags=["review"])


class FeedbackIn(BaseModel):
    rule_id: str = Field(min_length=1, max_length=16)
    stream_layer: str = "PLAYBACK"
    variant: str | None = None
    verdict: str
    note: str | None = Field(default=None, max_length=2000)


@router.post("/jobs/{job_id}/feedback")
async def give_feedback(job_id: str, body: FeedbackIn) -> dict[str, Any]:
    """Confirm a finding, or mark it a false positive. A second verdict replaces the first."""
    try:
        return await review.record_feedback(
            job_id=job_id,
            rule_id=body.rule_id,
            stream_layer=body.stream_layer,
            variant=body.variant,
            verdict=body.verdict,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/jobs/{job_id}/feedback")
async def job_feedback(job_id: str) -> dict[str, Any]:
    return {"feedback": await review.feedback_for_job(job_id)}


@router.get("/rules/precision")
async def precision() -> dict[str, Any]:
    """Per rule: confirmed and false-positive verdicts, and the share that were real."""
    return {"rules": await review.rule_precision()}


class AcknowledgementIn(BaseModel):
    # The channel, by its playback URL or by a run of it.
    playback_url: str | None = None
    job_id: str | None = None
    rule_id: str = Field(min_length=1, max_length=16)
    variant: str | None = None
    note: str = Field(min_length=3, max_length=2000)
    channel_name: str | None = None
    expires_days: int | None = Field(default=None, ge=1, le=3650)


@router.get("/acknowledgements")
async def list_acknowledgements(url: str | None = Query(default=None)) -> dict[str, Any]:
    return {"acknowledgements": await review.acknowledgements(url)}


@router.post("/acknowledgements", status_code=201)
async def acknowledge(body: AcknowledgementIn) -> dict[str, Any]:
    """Accept a known behaviour of one channel: it is still shown, no longer alerted on."""
    url = body.playback_url
    name = body.channel_name
    if not url and body.job_id:
        url, job_name = await _job_channel(body.job_id)
        name = name or job_name
    if not url:
        raise HTTPException(status_code=422, detail="Name the channel: playback_url or job_id")
    return await review.add_acknowledgement(
        playback_url=url,
        rule_id=body.rule_id,
        variant=body.variant,
        note=body.note,
        channel_name=name,
        expires_days=body.expires_days,
    )


@router.delete("/acknowledgements/{ack_id}")
async def unacknowledge(ack_id: int) -> dict[str, Any]:
    if not await review.remove_acknowledgement(ack_id):
        raise HTTPException(status_code=404, detail="No such acknowledgement")
    return {"deleted": True}


async def _job_channel(job_id: str) -> tuple[str | None, str | None]:
    from app.jobs.manager import job_manager

    handle = job_manager.get(job_id)
    if handle is not None and handle.urls.get("playback_url"):
        return handle.urls.get("playback_url"), handle.channel_name
    async with db_session.session_scope() as session:
        row = await session.get(JobRow, job_id)
    return (row.playback_url, row.channel_name) if row is not None else (None, None)


@router.get("/jobs/{job_id}/acknowledged")
async def job_acknowledged(job_id: str) -> dict[str, Any]:
    """The acknowledgements in force for the channel this run analysed."""
    url, _ = await _job_channel(job_id)
    return {
        "channel_key": review.channel_key(url or ""),
        "acknowledgements": await review.acknowledgements(url) if url else [],
    }
