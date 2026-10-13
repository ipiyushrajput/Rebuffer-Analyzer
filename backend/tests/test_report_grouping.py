"""A report lists an issue once, carries the playlist it fired on, and stays inside the page.

Three things a reader of a forwarded report needs and did not get:

- the same rule on four renditions was four rows (and four alarms in the escalation text);
- the "manifest snapshots around each incident" section was always empty, because nothing
  passed snapshots to the renderer, so no report ever showed where in a playlist an issue was;
- long URLs, tokens and User-Agent strings widened tables past the page.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.analysis.grouping import group_findings
from app.analysis.rules import catalogue as R
from app.db import session as db_session
from app.db.models import ManifestBody, ManifestPoll
from app.main import create_app
from app.reports.email_block import build_blocks
from tests.fixtures.server import FixtureServer, build_simple_channel


@pytest.fixture(autouse=True)
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


def _missing(variant: str, count: int) -> object:
    finding = R.HTTP_404_LISTED.raise_finding(
        f"segment https://cdn.example/{variant}/seg5.ts returned HTTP 404.",
        evidence={"url": f"https://cdn.example/{variant}/seg5.ts", "status": 404},
        variant=variant,
    )
    finding.count = count
    return finding


def test_the_same_rule_on_several_renditions_is_one_group_worst_first() -> None:
    findings = [_missing("low", 2), _missing("high", 5), _missing("mid", 1)]
    groups = group_findings(findings)  # type: ignore[arg-type]
    assert len(groups) == 1
    group = groups[0]
    assert group.count == 8
    assert [m.variant for m in group.members] == ["high", "low", "mid"]
    assert group.rendition_label() == "high (5), low (2), mid (1)"


def test_the_escalation_lists_an_issue_once_with_its_renditions_and_a_curl() -> None:
    findings = [_missing("low", 2), _missing("high", 5)]
    blocks = build_blocks(
        findings,  # type: ignore[arg-type]
        channel_name="Channel",
        window_start=dt.datetime(2026, 10, 13, 9, tzinfo=dt.UTC),
        window_end=dt.datetime(2026, 10, 13, 10, tzinfo=dt.UTC),
        playback_url="https://cdn.example/master.m3u8",
        rebuffer_ratio=None,
        threshold=0.02,
        user_agent="Mozilla/5.0 (SMART-TV; Tizen 9.0)",
    )
    assert len(blocks) == 1
    block = blocks[0]
    assert block.finding_count == 1
    assert block.body.count("HTTP-005") == 1
    assert "Renditions: high (5), low (2)" in block.body
    assert "Reproduce: curl " in block.body
    assert "Tizen 9.0" in block.body


@pytest.mark.asyncio()
async def test_a_report_shows_each_renditions_playlist_with_the_missing_segment_marked(
    origin: FixtureServer,
) -> None:
    from app.api.schemas import JobOptionsIn
    from app.jobs.manager import job_manager
    from app.reports.render_html import render_html_for_handle
    from app.reports.service import _manifests_for

    url = build_simple_channel(origin, variant_count=2, segment_count=6)
    origin.remove("low-seg2.ts")
    origin.remove("mid-seg2.ts")
    await job_manager.start()
    handle = await job_manager.submit(
        job_type="realtime",
        playback_url=url,
        channel_name="Report channel",
        options=JobOptionsIn().to_session_options(duration_s=5.0, default_record=False),
    )
    assert handle.task is not None
    await handle.task
    try:
        html = render_html_for_handle(
            handle, handle.result, await _manifests_for(handle, handle.result)
        )
        # One row for the missing segment, naming both renditions.
        row = html[html.index(">HTTP-005</code>") :]
        row = row[: row.index("</tr>")]
        assert html.count(">HTTP-005</code>") == 1
        assert "2 rendition(s)" in row
        # Each rendition carries its own playlist at the time, with the segment marked.
        marked = re.findall(
            r'class="ln hl"[^>]*><span class="n">\d+</span><span class="t">([^<]*)</span>', row
        )
        assert any("low-seg2.ts" in line for line in marked), marked
        assert any("mid-seg2.ts" in line for line in marked), marked
        assert "the poll the finding fired on" in row
        # Nothing long is allowed to widen the page.
        assert "overflow-wrap: anywhere" in html
    finally:
        async with db_session.session_scope() as session:
            for model in (ManifestPoll, ManifestBody):
                await session.execute(delete(model).where(model.job_id == handle.id))


@pytest.mark.asyncio()
async def test_an_incident_carries_the_playlist_current_when_it_began() -> None:
    from app.analysis.correlate import Incident
    from app.jobs.manifest_log import ManifestLog
    from app.net.fetcher import FetchResult, Timings
    from app.reports.manifests import incident_manifests

    job = "incident-manifest"
    start = dt.datetime(2026, 10, 13, 9, 0, tzinfo=dt.UTC)
    log = ManifestLog(job_id=job)
    for index in range(3):
        body = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
            f"#EXT-X-MEDIA-SEQUENCE:{index}\n#EXTINF:6.0,\nseg{index}.ts\n"
        ).encode()
        await log.record(
            seq=log.next_seq(),
            at=start + dt.timedelta(seconds=6 * index),
            layer="PLAYBACK",
            variant="v720",
            kind="video",
            url="https://cdn.example/v720.m3u8",
            result=FetchResult(
                requested_url="https://cdn.example/v720.m3u8",
                final_url="https://cdn.example/v720.m3u8",
                status=200,
                headers={},
                body=body,
                timings=Timings(total_ms=10.0),
            ),
            msn=index,
        )
    await log.flush()
    try:
        incident = Incident(
            kind="stale_playlist",
            variant="v720",
            started_at=start + dt.timedelta(seconds=7),
            ended_at=start + dt.timedelta(seconds=20),
            cause_rule_ids=["MED-004"],
            detail={"msn": 1},
        )
        excerpts = await incident_manifests(job, incident, [])
        assert len(excerpts) == 1
        excerpt = excerpts[0]
        assert excerpt["seq"] == 2, "the poll at or before the moment it began"
        assert [line["text"] for line in excerpt["lines"] if line["reason"]] == [
            "#EXTINF:6.0,",
            "seg1.ts",
        ]
    finally:
        async with db_session.session_scope() as session:
            for model in (ManifestPoll, ManifestBody):
                await session.execute(delete(model).where(model.job_id == job))
