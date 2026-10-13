"""One timeline for a run: polls, segment fetches, findings and incidents, in the order they happened."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import session as db_session
from app.db.models import ManifestBody, ManifestPoll
from app.main import create_app
from tests.fixtures.server import FixtureServer, build_simple_channel


@pytest.fixture(autouse=True)
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


@pytest.mark.asyncio()
async def test_a_missing_segment_reads_in_order_with_the_polls_and_the_finding(
    origin: FixtureServer,
) -> None:
    from app.api import reports as reports_api
    from app.api.schemas import JobOptionsIn
    from app.jobs.manager import job_manager

    url = build_simple_channel(origin, variant_count=1, segment_count=6)
    origin.remove("low-seg2.ts")
    await job_manager.start()
    handle = await job_manager.submit(
        job_type="realtime",
        playback_url=url,
        channel_name="Timeline channel",
        options=JobOptionsIn().to_session_options(duration_s=4.0, default_record=False),
    )
    assert handle.task is not None
    await handle.task
    try:
        start = (handle.started_at - dt.timedelta(seconds=5)).isoformat()
        end = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=5)).isoformat()
        timeline = await reports_api.job_timeline(
            handle.id, start=start, end=end, variant=None, limit=1500
        )
        events = timeline["events"]
        kinds = {event["kind"] for event in events}
        assert {"poll", "segment", "finding"} <= kinds
        assert [e["at"] for e in events] == sorted(e["at"] for e in events)
        missing = [e for e in events if e["kind"] == "segment" and e["status"] == 404]
        assert missing and missing[0]["ok"] is False
        assert "low-seg2.ts" in missing[0]["detail"]
        assert any(e["kind"] == "finding" and e["rule_id"] == "HTTP-005" for e in events)
        assert timeline["truncated"] == []

        limited = await reports_api.job_timeline(
            handle.id, start=start, end=end, variant=None, limit=2
        )
        assert "polls" in limited["truncated"], "a source cut short says so"

        profile = await reports_api.job_request_profile(handle.id)
        assert "Tizen" in profile["user_agent"] or profile["user_agent"]
    finally:
        async with db_session.session_scope() as session:
            for model in (ManifestPoll, ManifestBody):
                await session.execute(delete(model).where(model.job_id == handle.id))
