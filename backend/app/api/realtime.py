"""Realtime sessions.

A session starts an analysis immediately and streams every measurement over
`/ws/realtime/{id}`. A report can be generated at any moment from the data collected so
far, without stopping the session.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.api.proxy import proxied
from app.api.schemas import RealtimeSessionIn
from app.jobs.manager import job_manager

router = APIRouter(prefix="/realtime", tags=["realtime"])


@router.post("/sessions", status_code=201)
async def create_session(payload: RealtimeSessionIn) -> dict[str, Any]:
    options = payload.options.to_session_options(
        duration_s=payload.max_duration_minutes * 60, default_record=False
    )
    handle = await job_manager.submit(
        job_type="realtime",
        playback_url=payload.playback_url,
        origin_url=payload.origin_url,
        cdn_url=payload.cdn_url,
        ssai_url=payload.ssai_url,
        channel_name=payload.channel_name,
        options=options,
    )
    return {
        **handle.summary(),
        "ws_url": f"/ws/realtime/{handle.id}",
        # The player loads through the proxy: a browser cannot disable TLS verification and
        # CDN responses often carry no CORS headers.
        "player_url": proxied(payload.playback_url) + f"&session={handle.id}",
        "player_metrics_note": "Player metrics are measured from the analyzer host.",
    }


@router.get("/sessions")
async def list_sessions() -> dict[str, Any]:
    return {"sessions": [h.summary() for h in job_manager.list(job_type="realtime")]}


@router.get("/sessions/{session_id}")
async def read_session(session_id: str) -> dict[str, Any]:
    handle = job_manager.get(session_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return handle.summary()


@router.delete("/sessions/{session_id}")
async def stop_session(session_id: str) -> dict[str, Any]:
    handle = await job_manager.cancel(session_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return handle.summary()


@router.get("/sessions/{session_id}/result")
async def read_result(session_id: str) -> dict[str, Any]:
    """The full result. Available while the session runs, from what has been collected."""
    handle = job_manager.get(session_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if handle.result is not None:
        return handle.result.as_dict()
    if handle.session is None:
        raise HTTPException(status_code=409, detail="The session has not started collecting yet")
    return _interim(handle)


def _interim(handle: Any) -> dict[str, Any]:
    """A snapshot of a running session, shaped like a finished result."""
    from app.analysis import attribution, correlate
    from app.analysis.verdict import build as build_verdict

    session = handle.session
    findings = session.collector.all()
    findings, diffs = attribution.attribute(findings, layers_analysed=set(session.layers.keys()))
    target_duration = 6.0
    for context in session.layers.values():
        for value in context.target_duration_by_variant.values():
            if value:
                target_duration = value
                break
    chains = correlate.correlate(
        session.stalls, findings, target_duration=target_duration, thresholds=session.thresholds
    )
    playlists = sum(c.playlist_count for c in session.layers.values())
    segments = sum(c.segment_count for c in session.layers.values())
    verdict = build_verdict(
        findings=findings,
        chains=chains,
        incidents=session.incidents.incidents,
        thresholds=session.thresholds,
        window_seconds=session.elapsed_s,
        playlists_checked=playlists,
        segments_checked=segments,
    )
    return {
        "interim": True,
        "verdict": verdict.as_dict(),
        "findings": [f.as_dict() for f in findings],
        "incidents": [i.as_dict() for i in session.incidents.incidents],
        "chains": [c.as_dict() for c in chains],
        "layer_diffs": [d.as_dict() for d in diffs],
        "vpb": {
            variant: buffer.result.as_dict()
            for context in session.layers.values()
            for variant, buffer in context.buffers.items()
        },
        "started_at": session.started_at.isoformat(),
        "thresholds": session.thresholds.model_dump(mode="json"),
        "options": session.options.public(),
        "ladder": session._ladder_table(),
        "redirect_chains": session.redirect_chains,
        "event_log": session.event_log[-500:],
        "owner_summary": attribution.owner_summary(findings),
    }


@router.get("/sessions/{session_id}/manifests")
async def read_manifests(
    session_id: str, variant: str | None = Query(default=None)
) -> dict[str, Any]:
    """The latest raw playlist text per rendition, for the manifest viewers."""
    handle = job_manager.get(session_id)
    if handle is None or handle.session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    manifests: list[dict[str, Any]] = []
    for context in handle.session.layers.values():
        if context.master is not None:
            manifests.append(
                {
                    "layer": context.layer.value,
                    "variant": "master",
                    "url": context.master.final_url,
                    "raw": context.master.raw,
                    "at": handle.session.started_at.isoformat(),
                }
            )
        for name, target in context.targets.items():
            if variant and name != variant:
                continue
            latest = target.latest
            if latest is None or not latest.raw:
                continue
            manifests.append(
                {
                    "layer": context.layer.value,
                    "variant": name,
                    "kind": target.kind,
                    "url": latest.result.final_url,
                    "raw": latest.raw,
                    "at": latest.at.isoformat(),
                }
            )
    return {"manifests": manifests}


@router.get("/sessions/{session_id}/snapshots")
async def read_snapshots(
    session_id: str,
    variant: str = Query(...),
    at: str | None = Query(default=None, description="ISO-8601 moment to travel to"),
) -> dict[str, Any]:
    """The playlist exactly as it was fetched at a moment, with a diff against the one before."""
    import datetime as dt
    import difflib

    handle = job_manager.get(session_id)
    if handle is None or handle.session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    target = None
    for context in handle.session.layers.values():
        if variant in context.targets:
            target = context.targets[variant]
            break
    if target is None:
        raise HTTPException(status_code=404, detail=f"No rendition named {variant}")

    history = [s for s in target.history if s.raw]
    if not history:
        raise HTTPException(status_code=404, detail="No snapshot has been captured yet")

    index = len(history) - 1
    if at:
        try:
            moment = dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="at must be ISO-8601") from exc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.UTC)
        index = min(
            range(len(history)), key=lambda i: abs((history[i].at - moment).total_seconds())
        )

    chosen = history[index]
    previous = history[index - 1] if index > 0 else None
    diff = (
        list(
            difflib.unified_diff(
                previous.raw.splitlines(),
                chosen.raw.splitlines(),
                fromfile=f"{variant} @ {previous.at.isoformat()}",
                tofile=f"{variant} @ {chosen.at.isoformat()}",
                lineterm="",
            )
        )
        if previous
        else []
    )
    return {
        "variant": variant,
        "at": chosen.at.isoformat(),
        "url": chosen.result.final_url,
        "raw": chosen.raw,
        "summary": chosen.summary(),
        "diff": diff,
        "available": [s.at.isoformat() for s in history],
    }
