"""A run that nobody watched still has something to show afterwards.

Realtime draws its charts from the live socket. An aging run has no socket anyone is watching,
so unless its polls and fetches are written down as they happen, it finishes with findings and
nothing to look at — every chart on the Analytics panel empty whatever range is asked for.

That was the state of it: `samples_playlist`, `samples_segment` and `samples_player` were
declared, indexed, read by the charts and purged by retention, and nothing anywhere inserted a
row into any of them. What is asserted here is that a run records what it measured.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.db import session as db_session
from app.db.models import PlayerSample, PlaylistSample, SegmentSample
from app.jobs.samples import BATCH_ROWS, SampleRecorder, playlist_row, segment_row
from app.main import create_app

JOB = "recorder-test"


@pytest.fixture(autouse=True)
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


async def _clear() -> None:
    async with db_session.session_scope() as session:
        for model in (PlaylistSample, SegmentSample, PlayerSample):
            await session.execute(delete(model).where(model.job_id == JOB))


async def _count(model: type) -> int:
    async with db_session.session_scope() as session:
        total = await session.scalar(
            select(func.count()).select_from(model).where(model.job_id == JOB)
        )
    return int(total or 0)


def _snapshot(msn: int) -> dict[str, object]:
    """The payload the engine emits for one playlist poll."""
    return {
        "layer": "PLAYBACK",
        "data": {
            "at": dt.datetime(2026, 9, 21, 10, 0, msn % 60, tzinfo=dt.UTC).isoformat(),
            "variant": "v1080p@6046k",
            "url": "https://cdn.example/1080p/index.m3u8",
            "status": 200,
            "msn": msn,
            "last_msn": msn + 5,
            "dsn": 3,
            "segments": 6,
            "window_s": 36.0,
            "target_duration": 6.0,
            "ttfb_ms": 42.0,
            "total_ms": 88.0,
            "bytes": 940,
            "freshness_s": 2.5,
            "state": "LIVE",
        },
    }


def _segment(msn: int) -> dict[str, object]:
    return {
        "layer": "PLAYBACK",
        "data": {
            "at": dt.datetime(2026, 9, 21, 10, 0, msn % 60, tzinfo=dt.UTC).isoformat(),
            "variant": "v1080p@6046k",
            "msn": msn,
            "uri": f"https://cdn.example/1080p/seg_{msn}.ts",
            "status": 200,
            "bytes": 3_700_000,
            "download_ms": 1180.0,
            "ttfb_ms": 51.0,
            "declared_duration": 6.04,
            "actual_duration": 6.05,
            "measured_kbps": 4900.0,
            "av_skew_ms": 12.0,
            "container": "ts",
            "starts_with_keyframe": True,
        },
    }


# -- the rows a payload becomes ----------------------------------------------


def test_a_playlist_poll_keeps_every_field_the_charts_read() -> None:
    row = playlist_row(JOB, "PLAYBACK", _snapshot(882800)["data"])  # type: ignore[arg-type]

    assert (row.msn, row.last_msn, row.dsn, row.seg_count) == (882800, 882805, 3, 6)
    assert (row.ttfb_ms, row.total_ms, row.bytes) == (42.0, 88.0, 940)
    assert (row.window_s, row.target_duration) == (36.0, 6.0)
    assert row.freshness_s == 2.5, "freshness rides the snapshot; a zero would be a false flat"
    assert row.state == "LIVE"
    assert row.ts == dt.datetime(2026, 9, 21, 10, 0, 20, tzinfo=dt.UTC)


def test_a_segment_fetch_keeps_every_field_the_charts_read() -> None:
    row = segment_row(JOB, "PLAYBACK", _segment(882801)["data"])  # type: ignore[arg-type]

    assert (row.msn, row.http_status, row.bytes) == (882801, 200, 3_700_000)
    assert (row.download_ms, row.ttfb_ms) == (1180.0, 51.0)
    assert (row.duration_declared, row.duration_actual) == (6.04, 6.05)
    assert (row.measured_kbps, row.av_skew_ms) == (4900.0, 12.0)
    assert row.detail == {"container": "ts", "starts_with_keyframe": True}
    assert row.uri_hash, "the hash is not null, which the column requires"


def test_the_sample_timestamp_is_the_one_the_engine_stamped() -> None:
    """A row stamped on arrival would put the recorder's own lag into the measurement."""
    row = playlist_row(JOB, "PLAYBACK", {"at": "2026-01-02T03:04:05Z", "variant": "v720p@3000k"})

    assert row.ts == dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)


# -- what reaches the database -----------------------------------------------


@pytest.mark.asyncio()
async def test_a_run_that_records_leaves_its_polls_and_fetches_behind() -> None:
    """The defect this exists to prevent: an aging run that stored nothing to draw."""
    await _clear()
    recorder = SampleRecorder(JOB)
    try:
        for msn in range(10):
            await recorder.observe("playlist_snapshot", _snapshot(msn))
            await recorder.observe("segment_result", _segment(msn))
        await recorder.flush()

        assert await _count(PlaylistSample) == 10
        assert await _count(SegmentSample) == 10
        assert recorder.written == 20
        assert recorder.failed_flushes == 0
    finally:
        await _clear()


@pytest.mark.asyncio()
async def test_a_full_batch_is_written_without_waiting_for_the_run_to_end() -> None:
    """
    A seven-day run must not hold a week of samples in memory.

    The batch going out on its own is also what lets a reloaded tab see a run's charts fill
    while it is still running.
    """
    await _clear()
    recorder = SampleRecorder(JOB)
    try:
        for msn in range(BATCH_ROWS):
            await recorder.observe("playlist_snapshot", _snapshot(msn))

        assert await _count(PlaylistSample) == BATCH_ROWS, "flushed before any call to flush()"
    finally:
        await _clear()


@pytest.mark.asyncio()
async def test_a_payload_that_is_not_a_sample_is_ignored() -> None:
    await _clear()
    recorder = SampleRecorder(JOB)
    await recorder.observe("finding", {"data": {"rule_id": "SEG-008"}})
    await recorder.observe("status", {"state": "RUNNING"})
    await recorder.observe("playlist_snapshot", {"data": "not a dict"})
    await recorder.flush()

    assert recorder.written == 0
    assert await _count(PlaylistSample) == 0


# -- which runs record -------------------------------------------------------


def test_an_aging_run_records_and_a_realtime_session_does_not(api: TestClient) -> None:
    """
    Realtime has the socket, so it has no need to store what it is already drawing; aging has
    no socket and needs every sample it takes. `record_evidence` carries that distinction and
    is what the recorder is switched on by.
    """
    from app.api.schemas import JobOptionsIn

    aging = JobOptionsIn().to_session_options(duration_s=3600, default_record=True)
    realtime = JobOptionsIn().to_session_options(duration_s=300, default_record=False)

    assert aging.record_evidence is True
    assert realtime.record_evidence is False


def test_an_operator_can_ask_a_realtime_session_to_record() -> None:
    from app.api.schemas import JobOptionsIn

    options = JobOptionsIn(record_evidence=True).to_session_options(
        duration_s=300, default_record=False
    )

    assert options.record_evidence is True


# -- against a real run ------------------------------------------------------


@pytest.mark.asyncio()
async def test_a_real_job_records_what_it_measured(origin) -> None:  # type: ignore[no-untyped-def]
    """
    End to end through the job manager, which is where the recorder is switched on.

    A short aging run against the fixture origin has to leave playlist polls and segment
    fetches behind, because that is all the Analytics panel has to draw once the run is over.
    """
    from app.api.schemas import JobOptionsIn
    from app.jobs.manager import job_manager
    from tests.fixtures.server import build_simple_channel

    url = build_simple_channel(origin, variant_count=2, segment_count=6)
    await job_manager.start()

    handle = await job_manager.submit(
        job_type="aging",
        playback_url=url,
        channel_name="Recorded channel",
        options=JobOptionsIn().to_session_options(duration_s=5.0, default_record=True),
    )
    try:
        assert handle.task is not None
        await handle.task

        assert handle.recorder is not None
        assert handle.recorder.failed_flushes == 0

        async with db_session.session_scope() as session:
            polls = int(
                await session.scalar(
                    select(func.count())
                    .select_from(PlaylistSample)
                    .where(PlaylistSample.job_id == handle.id)
                )
                or 0
            )
            fetches = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SegmentSample)
                    .where(SegmentSample.job_id == handle.id)
                )
                or 0
            )
            first = (
                await session.execute(
                    select(PlaylistSample).where(PlaylistSample.job_id == handle.id).limit(1)
                )
            ).scalar_one()

        assert polls > 0, "the run polled playlists and recorded none"
        assert fetches > 0, "the run fetched segments and recorded none"
        assert first.variant, "each row names the rendition it measured"
        assert first.target_duration, "the poll carries what it read from the playlist"
    finally:
        async with db_session.session_scope() as session:
            for model in (PlaylistSample, SegmentSample):
                await session.execute(delete(model).where(model.job_id == handle.id))
