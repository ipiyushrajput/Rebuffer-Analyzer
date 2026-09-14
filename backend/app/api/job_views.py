"""Reading a stored job back out of the database.

A finished analysis outlives the process that produced it: the job row, its findings, its
incidents and its virtual-buffer series are all persisted. The Aging tab and the Analysed
channels tab both read jobs back this way, so the shaping lives here once rather than in
each router.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Finding as FindingRow
from app.db.models import Incident as IncidentRow
from app.db.models import Job as JobRow
from app.db.models import VirtualBufferSample

TERMINAL = ("COMPLETED", "CANCELLED", "FAILED")


def row_summary(row: JobRow) -> dict[str, Any]:
    """A job row in the same shape the live job manager reports."""
    return {
        "id": row.id,
        "type": row.type,
        "status": row.status,
        "channel_name": row.channel_name or "(unnamed channel)",
        "urls": {"playback_url": row.playback_url},
        "options": row.params or {},
        "progress": 1.0 if row.status in TERMINAL else row.progress,
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


def finding_payload(row: FindingRow) -> dict[str, Any]:
    return {
        "rule_id": row.rule_id,
        "title": row.title,
        "layer": row.layer,
        "stream_layer": row.stream_layer,
        "severity": row.severity,
        "owner": row.owner,
        "owner_label": row.owner,
        "variant": row.variant,
        "detail": row.detail,
        "root_cause": row.root_cause,
        "fix": row.fix,
        "rebuffer_impact": row.rebuffer_impact,
        "count": row.count,
        "first_seen": row.first_seen.isoformat(),
        "last_seen": row.last_seen.isoformat(),
        "evidence": row.evidence,
        "layer_presence": row.layer_presence or {},
        "reference": "",
    }


def incident_payload(row: IncidentRow) -> dict[str, Any]:
    return {
        "kind": row.kind,
        "variant": row.variant,
        "started_at": row.started_at.isoformat(),
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "duration_s": row.duration_s,
        "cause_rule_ids": row.cause_finding_ids,
        "cause_chain": row.cause_chain,
        "detail": row.detail,
    }


async def stored_result(session: AsyncSession, row: JobRow) -> dict[str, Any]:
    """The stored findings, incidents and virtual-buffer series for one job."""
    findings = (
        (await session.execute(select(FindingRow).where(FindingRow.job_id == row.id)))
        .scalars()
        .all()
    )
    incidents = (
        (await session.execute(select(IncidentRow).where(IncidentRow.job_id == row.id)))
        .scalars()
        .all()
    )
    buffer_rows = (
        (
            await session.execute(
                select(VirtualBufferSample)
                .where(VirtualBufferSample.job_id == row.id)
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
        "findings": [finding_payload(f) for f in findings],
        "incidents": [incident_payload(i) for i in incidents],
        "vpb": {variant: {"series": points} for variant, points in series.items()},
    }
