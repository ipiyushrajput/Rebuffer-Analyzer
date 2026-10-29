"""Engineers' verdicts on findings, and a channel's accepted behaviours.

A verdict is kept per finding and summed per rule into its precision. An acknowledgement keeps
a finding measured and shown and takes it out of escalations, for one channel only, and a new
token on the same channel is still the same channel.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.analysis import review
from app.analysis.rules import catalogue as R
from app.db import session as db_session
from app.db.models import Acknowledgement, FindingFeedback
from app.main import create_app
from app.reports.email_block import build_blocks


@pytest.fixture(autouse=True)
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


async def _clear() -> None:
    async with db_session.session_scope() as session:
        await session.execute(
            delete(FindingFeedback).where(FindingFeedback.job_id.like("review-%"))
        )
        await session.execute(
            delete(Acknowledgement).where(
                Acknowledgement.channel_key.like("https://review.example%")
            )
        )


def test_a_channel_is_the_same_channel_whatever_its_token() -> None:
    a = review.channel_key(
        "https://CDN.example/live/ch1/master.m3u8?hdnts=exp=1~hmac=aa|COMPONENT=HLS"
    )
    b = review.channel_key("https://cdn.example/live/ch1/master.m3u8?hdnts=exp=2~hmac=bb")
    assert a == b == "https://cdn.example/live/ch1/master.m3u8"
    assert review.channel_key("https://cdn.example/live/ch2/master.m3u8") != a


@pytest.mark.asyncio()
async def test_a_verdict_replaces_the_previous_one_and_sums_into_precision() -> None:
    await _clear()
    try:
        base = {"rule_id": "HTTP-005", "stream_layer": "PLAYBACK", "note": None}
        await review.record_feedback(
            job_id="review-1", variant="v1", verdict="false_positive", **base
        )
        await review.record_feedback(job_id="review-1", variant="v1", verdict="confirmed", **base)
        await review.record_feedback(job_id="review-1", variant="v2", verdict="confirmed", **base)
        await review.record_feedback(
            job_id="review-2", variant="v1", verdict="false_positive", **base
        )

        assert len(await review.feedback_for_job("review-1")) == 2, "one verdict per finding"
        precision = (await review.rule_precision())["HTTP-005"]
        assert precision["confirmed"] >= 2 and precision["false_positive"] >= 1
        assert 0 < precision["precision"] < 1

        with pytest.raises(ValueError):
            await review.record_feedback(job_id="review-1", variant="v1", verdict="maybe", **base)
    finally:
        await _clear()


@pytest.mark.asyncio()
async def test_an_acknowledgement_covers_its_channel_and_rendition_until_it_expires() -> None:
    await _clear()
    url = "https://review.example/live/ch/master.m3u8?token=1"
    try:
        await review.add_acknowledgement(
            playback_url=url, rule_id="MED-004", variant=None, note="known slow packager"
        )
        await review.add_acknowledgement(
            playback_url=url, rule_id="HTTP-005", variant="v1", note="v1 gaps accepted"
        )
        expired = await review.add_acknowledgement(
            playback_url=url, rule_id="SEG-002", variant=None, note="old", expires_days=1
        )
        async with db_session.session_scope() as session:
            row = await session.get(Acknowledgement, expired["id"])
            row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)

        matches = await review.matches_for("https://review.example/live/ch/master.m3u8?token=2")
        assert review.is_acknowledged(matches, "MED-004", "any-rendition")
        assert review.is_acknowledged(matches, "HTTP-005", "v1")
        assert not review.is_acknowledged(matches, "HTTP-005", "v2")
        assert not review.is_acknowledged(matches, "SEG-002", None), (
            "an expired one is not in force"
        )
        assert await review.matches_for("https://review.example/live/other/master.m3u8") == []
    finally:
        await _clear()


def test_an_acknowledged_finding_is_left_out_of_the_escalation() -> None:
    kept = R.HTTP_404_LISTED.raise_finding(
        "seg 404", evidence={"url": "https://x/a.ts"}, variant="v2"
    )
    accepted = R.HTTP_404_LISTED.raise_finding(
        "seg 404", evidence={"url": "https://x/b.ts"}, variant="v1"
    )
    matches = [review.AckMatch("HTTP-005", "v1")]
    escalated = [
        f for f in (kept, accepted) if not review.is_acknowledged(matches, f.rule.id, f.variant)
    ]
    blocks = build_blocks(
        escalated,
        channel_name="c",
        window_start=dt.datetime(2026, 10, 13, tzinfo=dt.UTC),
        window_end=dt.datetime(2026, 10, 13, 1, tzinfo=dt.UTC),
        playback_url="",
        rebuffer_ratio=None,
        threshold=0.0,
    )
    assert "Rendition: v2" in blocks[0].body
    assert "v1" not in blocks[0].body
