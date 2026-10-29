"""A finding opens on the playlist it fired on, with the lines at fault marked.

Opening a finding used to show the rendition's playlist as it stood *now*, so a segment that
went missing an hour ago was looked for in a window that had long since rolled past it. The
finding now names its poll, the poll log serves that exact body, and the lines the evidence
points at are highlighted: the segment by its media sequence number or URI, a master line by
the number the parser recorded, a tag by the name the finding's own sentence uses.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.analysis.rules import catalogue as R
from app.db import session as db_session
from app.db.models import ManifestBody, ManifestPoll
from app.hls.locate import locate
from app.main import create_app
from tests.fixtures.server import FixtureServer, build_simple_channel

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:100
#EXTINF:6.0,
seg_100.ts?token=a
#EXTINF:6.0,
seg_101.ts?token=a
#EXT-X-DISCONTINUITY
#EXTINF:6.0,
seg_102.ts?token=a
"""


@pytest.fixture(autouse=True)
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


def _lines(highlights: list) -> list[int]:  # type: ignore[type-arg]
    return [h.line for h in highlights]


def test_a_segment_is_found_by_its_media_sequence_number() -> None:
    marks = locate(MEDIA, evidence={"msn": 101})
    assert _lines(marks) == [7, 8]
    assert "MSN 101" in marks[0].reason


def test_a_segment_is_found_by_its_uri_whatever_its_token() -> None:
    marks = locate(
        MEDIA,
        evidence={"uri": "https://cdn.example/live/seg_102.ts?token=zzz"},
        base_url="https://cdn.example/live/index.m3u8",
    )
    assert _lines(marks) == [10, 11]


def test_a_tag_named_by_the_finding_is_marked() -> None:
    marks = locate(MEDIA, text="A discontinuity (EXT-X-DISCONTINUITY) is not aligned")
    assert _lines(marks) == [9]


def test_a_rendition_in_a_master_marks_its_stream_inf_line_too() -> None:
    master = (
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nlow/index.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=3000000\nhigh/index.m3u8\n"
    )
    marks = locate(
        master,
        evidence={"url": "https://cdn.example/high/index.m3u8"},
        base_url="https://cdn.example/master.m3u8",
    )
    assert _lines(marks) == [4, 5]


def test_a_line_the_parser_recorded_is_marked() -> None:
    marks = locate(MEDIA, evidence={"line": 4})
    assert _lines(marks) == [4]


def test_nothing_is_marked_when_nothing_names_a_line() -> None:
    assert locate(MEDIA, evidence={"stall_s": 3.2}, text="The player stalled") == []
    assert locate("", evidence={"msn": 1}) == []


def test_a_number_outside_the_window_marks_nothing() -> None:
    assert locate(MEDIA, evidence={"msn": 7}) == []


def test_a_finding_keeps_its_first_and_its_latest_occurrences() -> None:
    finding = R.HTTP_404_LISTED.raise_finding("first", evidence={"n": 0})
    for n in range(1, 10):
        finding.merge(R.HTTP_404_LISTED.raise_finding(f"occurrence {n}", evidence={"n": n}))
    assert [sample["n"] for sample in finding.evidence] == [0, 1, 7, 8, 9]
    assert finding.count == 10


@pytest.mark.asyncio()
async def test_a_missing_segment_opens_on_its_own_poll_with_its_lines_marked(
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
        channel_name="Locate channel",
        options=JobOptionsIn().to_session_options(duration_s=5.0, default_record=False),
    )
    assert handle.task is not None
    await handle.task
    try:
        finding = next(f for f in handle.result.findings if f.rule.id == "HTTP-005")
        sample = finding.evidence[0]
        assert sample["detail"], "each occurrence keeps its own sentence"

        served = await reports_api.locate_poll(
            handle.id,
            reports_api.LocateIn(
                seq=sample["poll"]["seq"],
                evidence=sample,
                text=f"{finding.rule.title} {finding.detail}",
            ),
        )
        assert served["exact"] is True
        assert served["seq"] == sample["poll"]["seq"]
        lines = served["raw"].splitlines()
        marked = [lines[h["line"] - 1] for h in served["highlights"]]
        assert any("low-seg2.ts" in line for line in marked), marked
        assert served["curl"].startswith("curl ")

        # Without a poll number, the rendition's poll current at the moment is served.
        nearest = await reports_api.locate_poll(
            handle.id,
            reports_api.LocateIn(variant=finding.variant, at=sample["poll"]["at"]),
        )
        assert nearest["seq"] == sample["poll"]["seq"]
        assert nearest["exact"] is False
    finally:
        async with db_session.session_scope() as session:
            for model in (ManifestPoll, ManifestBody):
                await session.execute(delete(model).where(model.job_id == handle.id))


@pytest.mark.asyncio()
async def test_time_travel_reads_the_poll_log_once_the_session_is_gone(
    origin: FixtureServer,
) -> None:
    from app.api.realtime import read_snapshots
    from app.api.schemas import JobOptionsIn
    from app.jobs.manager import job_manager

    url = build_simple_channel(origin, variant_count=1, segment_count=6)
    await job_manager.start()
    handle = await job_manager.submit(
        job_type="realtime",
        playback_url=url,
        channel_name="Travel channel",
        options=JobOptionsIn().to_session_options(duration_s=4.0, default_record=False),
    )
    assert handle.task is not None
    await handle.task
    variant = next(v for context in handle.session.layers.values() for v in context.targets)
    first_at = next(
        s.at for context in handle.session.layers.values() for s in context.targets[variant].history
    )
    try:
        handle.session = None  # as after a restart
        travelled = await read_snapshots(handle.id, variant=variant, at=first_at.isoformat())
        assert travelled["source"] == "poll_log"
        assert travelled["raw"].startswith("#EXTM3U")
    finally:
        async with db_session.session_scope() as session:
            for model in (ManifestPoll, ManifestBody):
                await session.execute(delete(model).where(model.job_id == handle.id))
