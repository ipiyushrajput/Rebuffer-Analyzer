"""One timeline for a stretch of a run: polls, segment fetches, player events, findings, incidents.

Cause and effect in a stream problem is a question of order — the playlist that stopped
advancing, the segment that came back 404, the buffer that ran dry, the stall — and each lives
in its own table. This reads all of them for a window and returns one list in time order, so an
engineer reads the incident the way the player lived it.

Bounded: every source is read for the window only, up to `limit` rows each, and the answer says
when a source was cut short rather than presenting a partial picture as a whole one.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select

from app.db import session as db_session
from app.db.models import Finding as FindingRow
from app.db.models import Incident as IncidentRow
from app.db.models import ManifestPoll, PlayerSample, SegmentSample

DEFAULT_LIMIT = 1500


def _iso(moment: dt.datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC).isoformat()


def _parse(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str) and value:
        try:
            moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)
    return None


def _inside(moment: dt.datetime | None, start: dt.datetime | None, end: dt.datetime | None) -> bool:
    if moment is None:
        return False
    return (start is None or moment >= start) and (end is None or moment <= end)


async def _rows(
    model: Any,
    job_id: str,
    start: dt.datetime | None,
    end: dt.datetime | None,
    variant: str | None,
    limit: int,
) -> tuple[list[Any], bool]:
    query = select(model).where(model.job_id == job_id)
    if start is not None:
        query = query.where(model.ts >= start)
    if end is not None:
        query = query.where(model.ts <= end)
    if variant and hasattr(model, "variant"):
        query = query.where(model.variant == variant)
    # Ordered on the (job_id, ts) index and read one past the limit, to know it was cut short.
    async with db_session.session_scope() as session:
        rows = list(
            (await session.execute(query.order_by(model.ts).limit(limit + 1))).scalars().all()
        )
    return rows[:limit], len(rows) > limit


def _finding_events(
    findings: list[dict[str, Any]],
    start: dt.datetime | None,
    end: dt.datetime | None,
    variant: str | None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for finding in findings:
        if variant and finding.get("variant") not in (variant, None):
            continue
        for sample in finding.get("evidence") or []:
            if not isinstance(sample, dict):
                continue
            found = sample.get("poll")
            poll: dict[str, Any] = found if isinstance(found, dict) else {}
            moment = _parse(poll.get("at") or sample.get("at"))
            if not _inside(moment, start, end):
                continue
            events.append(
                {
                    "at": _iso(moment),
                    "kind": "finding",
                    "layer": finding.get("stream_layer"),
                    "variant": finding.get("variant"),
                    "severity": finding.get("severity"),
                    "rule_id": finding.get("rule_id"),
                    "summary": f"{finding.get('rule_id')} {finding.get('title')}",
                    "detail": sample.get("detail") or finding.get("detail"),
                    "seq": poll.get("seq"),
                }
            )
    return events


async def _stored_findings(job_id: str) -> list[dict[str, Any]]:
    async with db_session.session_scope() as session:
        rows = (
            (await session.execute(select(FindingRow).where(FindingRow.job_id == job_id)))
            .scalars()
            .all()
        )
    return [
        {
            "rule_id": r.rule_id,
            "title": r.title,
            "severity": r.severity,
            "stream_layer": r.stream_layer,
            "variant": r.variant,
            "detail": r.detail,
            "evidence": r.evidence or [],
        }
        for r in rows
        if r.severity != "PASS"
    ]


async def _stored_incidents(job_id: str) -> list[dict[str, Any]]:
    async with db_session.session_scope() as session:
        rows = (
            (await session.execute(select(IncidentRow).where(IncidentRow.job_id == job_id)))
            .scalars()
            .all()
        )
    return [
        {
            "kind": r.kind,
            "variant": r.variant,
            "started_at": _iso(r.started_at),
            "ended_at": _iso(r.ended_at),
            "duration_s": r.duration_s,
            "cause_rule_ids": r.cause_finding_ids or [],
        }
        for r in rows
    ]


async def build_timeline(
    job_id: str,
    *,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    variant: str | None = None,
    live_findings: list[dict[str, Any]] | None = None,
    live_incidents: list[dict[str, Any]] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Every event of the window in time order, and which sources were cut short."""
    events: list[dict[str, Any]] = []
    truncated: list[str] = []

    polls, cut = await _rows(ManifestPoll, job_id, start, end, variant, limit)
    if cut:
        truncated.append("polls")
    for row in polls:
        ok = 200 <= (row.status or 0) < 300 and not row.error
        events.append(
            {
                "at": _iso(row.ts),
                "kind": "poll",
                "layer": row.stream_layer,
                "variant": row.variant,
                "status": row.status,
                "ok": ok,
                "ms": row.total_ms,
                "seq": row.seq,
                "msn": row.msn,
                "summary": (
                    f"poll #{row.seq} {row.variant} HTTP {row.status or '—'}"
                    + (f" MSN {row.msn}–{row.last_msn}" if row.msn is not None else "")
                ),
                "detail": row.error or "",
            }
        )

    segments, cut = await _rows(SegmentSample, job_id, start, end, variant, limit)
    if cut:
        truncated.append("segments")
    for row in segments:
        ok = 200 <= (row.http_status or 0) < 300
        events.append(
            {
                "at": _iso(row.ts),
                "kind": "segment",
                "layer": row.stream_layer,
                "variant": row.variant,
                "status": row.http_status,
                "ok": ok,
                "ms": row.download_ms,
                "msn": row.msn,
                "summary": f"segment MSN {row.msn} {row.variant} HTTP {row.http_status or '—'}",
                "detail": row.uri,
            }
        )

    players, cut = await _rows(PlayerSample, job_id, start, end, None, limit)
    if cut:
        truncated.append("player")
    for row in players:
        events.append(
            {
                "at": _iso(row.ts),
                "kind": "player",
                "variant": None,
                "summary": f"player {row.event}"
                + (f", buffer {row.buffer_s:.1f} s" if row.buffer_s is not None else "")
                + (f", stalled {row.stall_duration_s:.1f} s" if row.stall_duration_s else ""),
                "detail": "",
                "ok": row.event not in ("stall", "waiting", "error"),
            }
        )

    findings = live_findings if live_findings is not None else await _stored_findings(job_id)
    events += _finding_events(findings, start, end, variant)

    incidents = live_incidents if live_incidents is not None else await _stored_incidents(job_id)
    for incident in incidents:
        if variant and incident.get("variant") not in (variant, None):
            continue
        for edge, key in (("incident_start", "started_at"), ("incident_end", "ended_at")):
            moment = _parse(incident.get(key))
            if not _inside(moment, start, end):
                continue
            events.append(
                {
                    "at": _iso(moment),
                    "kind": edge,
                    "variant": incident.get("variant"),
                    "summary": f"{incident.get('kind')} incident "
                    + ("opens" if edge == "incident_start" else "closes"),
                    "detail": ", ".join(incident.get("cause_rule_ids") or []),
                    "ok": False,
                }
            )

    events.sort(key=lambda e: e["at"] or "")
    return {"events": events, "truncated": truncated}
