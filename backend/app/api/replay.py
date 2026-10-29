"""Re-analyse a finished run from what it stored, with the settings in force now."""

from __future__ import annotations

import dataclasses
from typing import Any

from fastapi import APIRouter, HTTPException

from app.analysis.replay import ReplaySource
from app.config import get_settings
from app.db import session as db_session
from app.db.models import Job as JobRow
from app.jobs.manager import _options_from_params, job_manager

router = APIRouter(tags=["replay"])

# The supervisor needs a moment past the last stored poll to hand it to the engine.
TAIL_S = 5.0
RUNNING = {"PENDING", "RUNNING"}


@router.post("/jobs/{job_id}/replay")
async def replay_job(job_id: str) -> dict[str, Any]:
    async with db_session.session_scope() as session:
        job = await session.get(JobRow, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No run {job_id}")
    if job.status in RUNNING:
        raise HTTPException(
            status_code=409, detail="The run is still going; replay it once it ends."
        )
    source = await ReplaySource.load(job_id, get_settings().rba_recordings_dir)
    if not source.available:
        raise HTTPException(
            status_code=409,
            detail="This run has no poll log to replay: it finished before playlist polls "
            "were written down, or its polls passed the retention period.",
        )

    params = dict(job.params or {})
    # A replay of a replay re-reads the original: the stored responses are the same.
    original = params.get("replay_of") or job_id
    if original != job_id:
        source = await ReplaySource.load(original, get_settings().rba_recordings_dir)
    options = dataclasses.replace(
        _options_from_params(params),
        duration_s=source.duration_s + TAIL_S,
        record_stream=False,
        replay_of=original,
    )
    name = (job.channel_name or "(unnamed channel)").removesuffix(" — replay")[:240]
    handle = await job_manager.submit(
        job_type=job.type if job.type in ("realtime", "aging") else "realtime",
        playback_url=job.playback_url or "",
        origin_url=job.origin_url,
        cdn_url=job.cdn_url,
        ssai_url=job.ssai_url,
        channel_name=f"{name} — replay",
        options=options,
        parent_job_id=original,
    )
    return {
        "job_id": handle.id,
        "replay_of": original,
        "duration_s": options.duration_s,
        "recorded": source.recorded,
        "playlists": sum(len(polls) for polls in source.playlists.values()),
        "segments": len(source.segments),
    }
