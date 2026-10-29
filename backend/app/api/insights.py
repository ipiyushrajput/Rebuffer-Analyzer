"""Across runs: search findings over every analysis, and compare two runs side by side.

**Search** answers "which channels had HTTP-005 this week", "everything CRITICAL on this CDN
host", "every run of this channel that raised anything". It reads a projection of the findings
and their jobs — never the evidence JSON — so the sort a newest-first listing needs stays inside
MySQL's sort buffer however long the history (see `app/db/paging.py`).

**Compare** puts two runs' issues side by side, grouped by rule and layer as everywhere else:
only in the first, only in the second, and in both with each run's count — the before and after
of a fix, two CDNs carrying one channel, two User-Agent profiles.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.db import session as db_session
from app.db.models import Finding as FindingRow
from app.db.models import Job as JobRow

router = APIRouter(tags=["insights"])

SEVERITY_RANK = {"CRITICAL": 4, "ERROR": 3, "WARN": 2, "INFO": 1, "PASS": 0}


@router.get("/search/findings")
async def search_findings(
    rule_id: str | None = Query(default=None, max_length=16),
    channel: str | None = Query(default=None, max_length=255),
    host: str | None = Query(default=None, max_length=255),
    severity: str | None = Query(default=None, pattern="^(CRITICAL|ERROR|WARN|INFO)$"),
    owner: str | None = Query(default=None, max_length=24),
    since_days: int = Query(default=7, ge=1, le=365),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """Findings across every run of the window, newest run first, one row per run and rule."""
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=since_days)
    query = (
        select(
            FindingRow.job_id,
            FindingRow.rule_id,
            FindingRow.title,
            FindingRow.severity,
            FindingRow.owner,
            FindingRow.stream_layer,
            FindingRow.variant,
            FindingRow.count.label("occurrences"),
            FindingRow.last_seen,
            JobRow.channel_name,
            JobRow.type,
            JobRow.created_at,
            JobRow.status,
        )
        .join(JobRow, JobRow.id == FindingRow.job_id)
        .where(JobRow.created_at >= since, FindingRow.severity != "PASS")
    )
    if rule_id:
        query = query.where(FindingRow.rule_id == rule_id.strip().upper())
    if channel:
        query = query.where(JobRow.channel_name.ilike(f"%{channel.strip()}%"))
    if host:
        query = query.where(JobRow.playback_url.ilike(f"%{host.strip()}%"))
    if owner:
        query = query.where(FindingRow.owner == owner.strip().upper())
    if severity:
        allowed = [name for name, rank in SEVERITY_RANK.items() if rank >= SEVERITY_RANK[severity]]
        query = query.where(FindingRow.severity.in_(allowed))
    # Every column selected is small: the evidence and the sentences are not in the sort.
    query = query.order_by(JobRow.created_at.desc(), FindingRow.rule_id).limit(limit * 8)
    async with db_session.session_scope() as session:
        rows = (await session.execute(query)).all()

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row.job_id, row.rule_id, row.stream_layer)
        entry = grouped.get(key)
        if entry is None:
            if len(grouped) >= limit:
                continue
            entry = {
                "job_id": row.job_id,
                "channel_name": row.channel_name,
                "job_type": row.type,
                "status": row.status,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "rule_id": row.rule_id,
                "title": row.title,
                "severity": row.severity,
                "owner": row.owner,
                "stream_layer": row.stream_layer,
                "renditions": [],
                "count": 0,
                "last_seen": row.last_seen.isoformat() if row.last_seen else None,
            }
            grouped[key] = entry
        entry["count"] += int(row.occurrences or 0)
        if row.variant and row.variant not in entry["renditions"]:
            entry["renditions"].append(row.variant)
        if SEVERITY_RANK.get(row.severity, 0) > SEVERITY_RANK.get(entry["severity"], 0):
            entry["severity"] = row.severity
    results = list(grouped.values())
    return {
        "results": results,
        "runs": len({r["job_id"] for r in results}),
        "channels": len({r["channel_name"] for r in results}),
        "since": since.isoformat(),
    }


async def _run_groups(job_id: str) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    async with db_session.session_scope() as session:
        job = await session.get(JobRow, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"No run {job_id}")
        rows = (
            await session.execute(
                select(
                    FindingRow.rule_id,
                    FindingRow.title,
                    FindingRow.severity,
                    FindingRow.owner,
                    FindingRow.stream_layer,
                    FindingRow.variant,
                    FindingRow.count.label("occurrences"),
                ).where(FindingRow.job_id == job_id, FindingRow.severity != "PASS")
            )
        ).all()
    params = job.params or {}
    summary = {
        "job_id": job.id,
        "channel_name": job.channel_name,
        "job_type": job.type,
        "status": job.status,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "playback_url": job.playback_url,
        "ua_profile": params.get("ua_profile"),
        "user_agent": params.get("user_agent"),
        "verdict_status": (job.verdict or {}).get("status"),
        "risk_score": (job.verdict or {}).get("risk_score"),
    }
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        entry = groups.setdefault(
            (row.rule_id, row.stream_layer),
            {
                "rule_id": row.rule_id,
                "stream_layer": row.stream_layer,
                "title": row.title,
                "severity": row.severity,
                "owner": row.owner,
                "count": 0,
                "renditions": [],
            },
        )
        entry["count"] += int(row.occurrences or 0)
        if row.variant and row.variant not in entry["renditions"]:
            entry["renditions"].append(row.variant)
        if SEVERITY_RANK.get(row.severity, 0) > SEVERITY_RANK.get(entry["severity"], 0):
            entry["severity"] = row.severity
    return summary, groups


@router.get("/compare")
async def compare_runs(a: str = Query(...), b: str = Query(...)) -> dict[str, Any]:
    """Two runs' issues side by side: only in A, only in B, and in both with each count."""
    run_a, groups_a = await _run_groups(a)
    run_b, groups_b = await _run_groups(b)

    def order(item: dict[str, Any]) -> tuple[int, str]:
        return (-SEVERITY_RANK.get(item["severity"], 0), item["rule_id"])

    only_a = sorted((groups_a[k] for k in groups_a.keys() - groups_b.keys()), key=order)
    only_b = sorted((groups_b[k] for k in groups_b.keys() - groups_a.keys()), key=order)
    both = sorted(
        (
            {
                **groups_a[k],
                "severity_a": groups_a[k]["severity"],
                "severity_b": groups_b[k]["severity"],
                "count_a": groups_a[k]["count"],
                "count_b": groups_b[k]["count"],
                "renditions_a": groups_a[k]["renditions"],
                "renditions_b": groups_b[k]["renditions"],
            }
            for k in groups_a.keys() & groups_b.keys()
        ),
        key=order,
    )
    return {"a": run_a, "b": run_b, "only_a": only_a, "only_b": only_b, "both": both}
