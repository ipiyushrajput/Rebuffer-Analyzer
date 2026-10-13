"""What engineers say about findings: verdicts, and a channel's accepted behaviours.

**Feedback** is a verdict on one finding — confirmed, or a false positive — summed per rule into
its precision: of the findings someone reviewed, the share that were real. The rules that raise
noise are then named by the people who read them, with numbers, in the rule catalogue.

**Acknowledgements** are a channel's known, accepted behaviours. The rule still fires, the
finding is still measured, stored and shown (marked), and the verdict is computed from every
finding exactly as before; an acknowledged finding is left out of alerts and escalations, so a
channel's accepted quirk stops paging people without the rule being turned off for everyone.
A channel is keyed by its playback URL without the query, so a new token is the same channel.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select

from app.db import session as db_session
from app.db.models import Acknowledgement, FindingFeedback

CONFIRMED = "confirmed"
FALSE_POSITIVE = "false_positive"
VERDICTS = (CONFIRMED, FALSE_POSITIVE)


def channel_key(url: str) -> str:
    """A channel's identity across tokens: scheme, host and path, the query and suffix dropped."""
    url = (url or "").split("|", 1)[0].strip()
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url.split("?", 1)[0][:255]
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"[:255]


# -- feedback ----------------------------------------------------------------


async def record_feedback(
    *,
    job_id: str,
    rule_id: str,
    stream_layer: str,
    variant: str | None,
    verdict: str,
    note: str | None,
) -> dict[str, Any]:
    """One verdict per finding: a second one replaces the first."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    async with db_session.session_scope() as session:
        row = await session.scalar(
            select(FindingFeedback).where(
                FindingFeedback.job_id == job_id,
                FindingFeedback.rule_id == rule_id,
                FindingFeedback.stream_layer == stream_layer,
                FindingFeedback.variant.is_(None)
                if variant is None
                else FindingFeedback.variant == variant,
            )
        )
        if row is None:
            row = FindingFeedback(
                job_id=job_id, rule_id=rule_id, stream_layer=stream_layer, variant=variant
            )
            session.add(row)
        row.verdict = verdict
        row.note = note or None
        row.created_at = dt.datetime.now(dt.UTC)
        await session.flush()
        return feedback_payload(row)


def feedback_payload(row: FindingFeedback) -> dict[str, Any]:
    return {
        "id": row.id,
        "job_id": row.job_id,
        "rule_id": row.rule_id,
        "stream_layer": row.stream_layer,
        "variant": row.variant,
        "verdict": row.verdict,
        "note": row.note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def feedback_for_job(job_id: str) -> list[dict[str, Any]]:
    async with db_session.session_scope() as session:
        rows = (
            (await session.execute(select(FindingFeedback).where(FindingFeedback.job_id == job_id)))
            .scalars()
            .all()
        )
    return [feedback_payload(row) for row in rows]


async def rule_precision() -> dict[str, dict[str, Any]]:
    """Per rule: how many reviewed findings were confirmed, how many were false positives."""
    async with db_session.session_scope() as session:
        rows = (
            await session.execute(
                select(FindingFeedback.rule_id, FindingFeedback.verdict, func.count()).group_by(
                    FindingFeedback.rule_id, FindingFeedback.verdict
                )
            )
        ).all()
    out: dict[str, dict[str, Any]] = {}
    for rule_id, verdict, count in rows:
        entry = out.setdefault(rule_id, {"confirmed": 0, "false_positive": 0})
        entry[verdict] = int(count)
    for entry in out.values():
        reviewed = entry["confirmed"] + entry["false_positive"]
        entry["reviewed"] = reviewed
        entry["precision"] = round(entry["confirmed"] / reviewed, 3) if reviewed else None
    return out


# -- acknowledgements --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AckMatch:
    rule_id: str
    variant: str | None

    def covers(self, rule_id: str, variant: str | None) -> bool:
        return self.rule_id == rule_id and (self.variant is None or self.variant == variant)


def ack_payload(row: Acknowledgement) -> dict[str, Any]:
    return {
        "id": row.id,
        "channel_key": row.channel_key,
        "channel_name": row.channel_name,
        "rule_id": row.rule_id,
        "variant": row.variant,
        "note": row.note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


async def add_acknowledgement(
    *,
    playback_url: str,
    rule_id: str,
    variant: str | None,
    note: str,
    channel_name: str | None = None,
    expires_days: int | None = None,
) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    async with db_session.session_scope() as session:
        row = Acknowledgement(
            channel_key=channel_key(playback_url),
            channel_name=channel_name,
            rule_id=rule_id,
            variant=variant,
            note=note,
            created_at=now,
            expires_at=now + dt.timedelta(days=expires_days) if expires_days else None,
        )
        session.add(row)
        await session.flush()
        return ack_payload(row)


async def remove_acknowledgement(ack_id: int) -> bool:
    async with db_session.session_scope() as session:
        row = await session.get(Acknowledgement, ack_id)
        if row is None:
            return False
        await session.delete(row)
    return True


async def acknowledgements(playback_url: str | None = None) -> list[dict[str, Any]]:
    """Every acknowledgement in force, or those of one channel."""
    now = dt.datetime.now(dt.UTC)
    query = select(Acknowledgement).where(
        or_(Acknowledgement.expires_at.is_(None), Acknowledgement.expires_at > now)
    )
    if playback_url:
        query = query.where(Acknowledgement.channel_key == channel_key(playback_url))
    async with db_session.session_scope() as session:
        rows = (await session.execute(query.order_by(Acknowledgement.id))).scalars().all()
    return [ack_payload(row) for row in rows]


async def matches_for(playback_url: str | None) -> list[AckMatch]:
    if not playback_url:
        return []
    try:
        return [AckMatch(a["rule_id"], a["variant"]) for a in await acknowledgements(playback_url)]
    except Exception:
        # A deployment that has not migrated has no table yet; nothing is acknowledged.
        return []


def is_acknowledged(matches: list[AckMatch], rule_id: str, variant: str | None) -> bool:
    return any(match.covers(rule_id, variant) for match in matches)
