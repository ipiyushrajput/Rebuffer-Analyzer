"""Reports API: generation, history, download and deletion."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.api.realtime import _interim
from app.jobs.manager import job_manager
from app.reports import service

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


@router.get("/jobs/{job_id}/snapshots")
async def job_snapshots(
    job_id: str, variant: str = Query(...), at: str | None = Query(default=None)
) -> dict[str, Any]:
    """Time travel for any job: the playlist exactly as it was fetched at a moment."""
    from app.api.realtime import read_snapshots

    return await read_snapshots(job_id, variant=variant, at=at)
