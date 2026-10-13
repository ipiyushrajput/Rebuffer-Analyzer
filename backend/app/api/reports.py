"""Reports API: generation, history, download and deletion."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from app.api.realtime import _interim
from app.hls.locate import locate
from app.jobs.manager import job_manager
from app.reports import network_log, service

router = APIRouter(tags=["reports"])


@router.post("/realtime/sessions/{session_id}/report")
async def realtime_report(
    session_id: str, format: Literal["html", "pdf"] = Query(default="html")
) -> dict[str, Any]:
    """Generate a report from what the session has collected so far, without stopping it."""
    handle = job_manager.get(session_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="Session not found")

    result = handle.result
    if result is None:
        if handle.session is None:
            raise HTTPException(
                status_code=409, detail="The session has not started collecting yet"
            )
        result = _snapshot_result(handle)

    return await service.generate(handle, fmt=format, result=result)


def _snapshot_result(handle: Any) -> Any:
    """Build an AnalysisResult from a running session so a report can be produced mid-flight."""
    from app.analysis.engine import AnalysisResult
    from app.analysis.verdict import Verdict, VerdictStatus

    interim = _interim(handle)
    session = handle.session

    findings = session.collector.all()
    verdict_data = interim["verdict"]
    verdict = Verdict(
        status=VerdictStatus(verdict_data["status"]),
        headline=verdict_data["headline"],
        primary=None,
        contributing=[],
        risk_score=verdict_data["risk_score"],
        owner=verdict_data["owner"],
        owner_label=verdict_data["owner_label"],
        required_fix=verdict_data["required_fix"],
        measured_rebuffer_ratio=verdict_data["measured_rebuffer_ratio"],
        worst_variant=verdict_data["worst_variant"],
        incident_count=verdict_data["incident_count"],
        incident_seconds=verdict_data["incident_seconds"],
        counts=verdict_data["counts"],
        window_seconds=verdict_data["window_seconds"],
        playlists_checked=verdict_data["playlists_checked"],
        segments_checked=verdict_data["segments_checked"],
    )

    from app.analysis import attribution, correlate

    _, diffs = attribution.attribute(findings, layers_analysed=set(session.layers.keys()))
    chains = correlate.correlate(
        session.stalls, findings, target_duration=6.0, thresholds=session.thresholds
    )

    return AnalysisResult(
        findings=findings,
        incidents=session.incidents.incidents,
        chains=chains,
        verdict=verdict,
        layer_diffs=diffs,
        vpb_results={
            variant: buffer.result
            for context in session.layers.values()
            for variant, buffer in context.buffers.items()
        },
        started_at=session.started_at,
        ended_at=dt.datetime.now(dt.UTC),
        thresholds=session.thresholds,
        options=session.options.public(),
        ladder=session._ladder_table(),
        redirect_chains=session.redirect_chains,
        event_log=session.event_log,
        owner_summary=attribution.owner_summary(findings),
        drm=session.drm.summary() if session.drm is not None else {},
    )


@router.get("/reports")
async def list_reports(
    channel: str | None = Query(default=None),
    owner: str | None = Query(default=None),
    verdict: str | None = Query(default=None),
    since: str | None = Query(default=None, description="ISO-8601 lower bound"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    moment: dt.datetime | None = None
    if since:
        try:
            moment = dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="since must be ISO-8601") from exc

    reports = await service.list_reports(
        channel=channel, owner=owner, verdict=verdict, since=moment, limit=limit
    )
    return {"reports": reports, "count": len(reports)}


@router.get("/reports/{report_id}")
async def read_report(report_id: int) -> dict[str, Any]:
    found = await service.get_report(report_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Report not found")
    data, media, filename = found
    return {
        "id": report_id,
        "media_type": media,
        "filename": filename,
        "size_bytes": len(data),
        "exists": True,
        "url": f"/api/reports/{report_id}/download",
    }


@router.get("/reports/{report_id}/download")
async def download_report(report_id: int, inline: bool = Query(default=False)) -> Response:
    """
    Serve a stored report, streamed from the database.

    ``inline=1`` drops the attachment disposition so the Reports tab renders the document in
    place. The bytes are identical either way; only the disposition header changes.
    """
    found = await service.get_report(report_id)
    if found is None:
        raise HTTPException(
            status_code=404, detail="Report not found, or its content is no longer stored"
        )
    data, media, filename = found
    disposition = "inline" if inline else "attachment"
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


@router.delete("/reports/{report_id}")
async def remove_report(report_id: int) -> dict[str, bool]:
    deleted = await service.delete_report(report_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Report not found")
    return {"deleted": True}


@router.get("/jobs/{job_id}/evidence.zip")
async def evidence_bundle(job_id: str) -> Response:
    """The evidence archive, built in memory from the stored rows and streamed."""
    handle = job_manager.get(job_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="Job not found")
    archive = await service.build_evidence_bundle(handle)
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="evidence-{job_id[:8]}.zip"'},
    )


# -- the poll log --------------------------------------------------------------


def _moment(value: str | None, name: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{name} must be ISO-8601") from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


async def _with_curl(job_id: str, poll: dict[str, Any]) -> dict[str, Any]:
    poll["curl"] = network_log.curl_command(poll["url"], await network_log.job_user_agent(job_id))
    return poll


@router.get("/jobs/{job_id}/network-log.zip")
async def network_log_archive(job_id: str) -> StreamingResponse:
    """Every playlist poll of the run as files, a HAR and a CSV, streamed as one ZIP.

    Read from the database, so it is complete for the whole run and is served after the run
    has finished and after a restart.
    """
    if await network_log.poll_count(job_id) == 0:
        raise HTTPException(
            status_code=404,
            detail="This run has no poll log: it ran before the poll log existed, or wrote none.",
        )
    return StreamingResponse(
        network_log.stream_archive(job_id),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="network-log-{job_id[:8]}.zip"'},
    )


@router.get("/jobs/{job_id}/polls")
async def job_polls(
    job_id: str,
    variant: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, Any]:
    """The poll log in poll order, a page at a time."""
    polls = await network_log.list_polls(
        job_id,
        variant=variant,
        start=_moment(start, "start"),
        end=_moment(end, "end"),
        after_seq=after_seq,
        limit=limit,
    )
    return {"polls": polls, "total": await network_log.poll_count(job_id)}


@router.get("/jobs/{job_id}/polls/{seq}")
async def job_poll(job_id: str, seq: int) -> dict[str, Any]:
    """One poll with its body and the rendition's previous poll, as a finding names it."""
    poll = await network_log.poll_with_context(job_id, seq=seq)
    if poll is None:
        raise HTTPException(status_code=404, detail=f"Poll {seq} is not in this run's log")
    return await _with_curl(job_id, poll)


@router.get("/jobs/{job_id}/poll-at")
async def job_poll_at(
    job_id: str, variant: str = Query(...), at: str | None = Query(default=None)
) -> dict[str, Any]:
    """The poll of a rendition current at a moment: the latest at or before it."""
    poll = await network_log.poll_with_context(job_id, variant=variant, at=_moment(at, "at"))
    if poll is None:
        raise HTTPException(status_code=404, detail=f"No poll of {variant} is in this run's log")
    return await _with_curl(job_id, poll)


class LocateIn(BaseModel):
    """What a finding knows about where it fired: its poll, or its rendition and moment."""

    seq: int | None = None
    variant: str | None = None
    at: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    # The finding's title and sentence, for the tags they name.
    text: str = ""


def _snapshot_payload(snapshot: Any, layer: str, variant: str, kind: str) -> dict[str, Any]:
    """An in-memory poll in the shape the poll log serves."""
    return {
        "seq": snapshot.seq,
        "at": snapshot.at.isoformat(),
        "layer": layer,
        "variant": variant,
        "kind": kind,
        "url": snapshot.url,
        "final_url": snapshot.result.final_url,
        "status": snapshot.result.status,
        "error": snapshot.result.error,
        "ttfb_ms": snapshot.result.timings.ttfb_ms,
        "total_ms": snapshot.result.timings.total_ms,
        "bytes": snapshot.result.bytes_received,
        "msn": snapshot.playlist.media_sequence if snapshot.playlist else None,
        "last_msn": snapshot.playlist.last_msn if snapshot.playlist else None,
        "headers": dict(snapshot.result.headers),
        "hops": [hop.as_dict() for hop in snapshot.result.hops],
        "timings": snapshot.result.timings.as_dict(),
        "body_hash": "",
        "pinned": False,
        "raw": snapshot.raw or snapshot.result.text,
    }


def _memory_poll(job_id: str, body: LocateIn) -> dict[str, Any] | None:
    """A poll the log has not flushed yet, from the live session's own history."""
    handle = job_manager.get(job_id)
    if handle is None or handle.session is None:
        return None
    moment = _moment(body.at, "at")
    for context in handle.session.layers.values():
        for variant, target in context.targets.items():
            history = [s for s in target.history if s.raw or s.result.body]
            if not history:
                continue
            chosen = None
            if body.seq is not None:
                chosen = next((s for s in history if s.seq == body.seq), None)
            elif body.variant == variant:
                before = [s for s in history if moment is None or s.at <= moment]
                chosen = (before or history)[-1]
            if chosen is None:
                continue
            index = history.index(chosen)
            previous = history[index - 1] if index > 0 else None

            layer = context.layer.value
            payload = _snapshot_payload(chosen, layer, variant, target.kind)
            payload["previous"] = (
                _snapshot_payload(previous, layer, variant, target.kind)
                if previous is not None
                else None
            )
            return payload
    return None


@router.post("/jobs/{job_id}/polls/locate")
async def locate_poll(job_id: str, body: LocateIn) -> dict[str, Any]:
    """The playlist a finding fired on, with the lines its evidence points at highlighted.

    The poll the finding names when it carries one; otherwise the rendition's poll current
    at the moment given. Never the live window: a finding is shown against the playlist as it
    stood when the rule fired.
    """
    poll = await network_log.poll_with_context(
        job_id, seq=body.seq, variant=body.variant, at=_moment(body.at, "at")
    )
    if poll is None or (body.seq is not None and poll.get("seq") != body.seq):
        poll = _memory_poll(job_id, body) or poll
    if poll is None:
        raise HTTPException(status_code=404, detail="No poll of this rendition is recorded")
    highlights = locate(
        poll.get("raw") or "",
        evidence=body.evidence,
        text=body.text,
        base_url=poll.get("final_url") or poll.get("url") or "",
    )
    poll["highlights"] = [h.as_dict() for h in highlights]
    # Whether this is the poll the finding named, or the nearest one to its moment.
    poll["exact"] = body.seq is not None and poll.get("seq") == body.seq
    return await _with_curl(job_id, poll)


@router.get("/jobs/{job_id}/snapshots")
async def job_snapshots(
    job_id: str, variant: str = Query(...), at: str | None = Query(default=None)
) -> dict[str, Any]:
    """Time travel for any job: the playlist exactly as it was fetched at a moment."""
    from app.api.realtime import read_snapshots

    return await read_snapshots(job_id, variant=variant, at=at)
