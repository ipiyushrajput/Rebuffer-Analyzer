"""A session that samples no segment must say why.

Zero sampled segments used to be indistinguishable from a session that was never asked to
sample: the poller swallowed every exception, and the screen showed an empty chart. These
tests hold the analyzer to explaining itself — and hold one crash to costing one segment
rather than the whole rung.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.analysis.collectors.playlist_poller import PlaylistPoller, PollTarget, Snapshot
from app.analysis.collectors.segment_sampler import RungSampling, SegmentSampler
from app.analysis.engine import AnalysisSession, SessionOptions
from app.net.fetcher import Fetcher
from tests.fixtures.server import FixtureServer, build_simple_channel


async def _run(url: str, *, duration: float = 4.0) -> AnalysisSession:
    session = AnalysisSession(
        session_id="test",
        playback_url=url,
        options=SessionOptions(duration_s=duration, max_segment_samples=60),
    )
    await session.run()
    return session


# -- the sampling account ----------------------------------------------------


async def test_a_session_that_samples_segments_offers_no_excuse(origin: FixtureServer) -> None:
    """`reason` is for the zero case only; a working session leaves it empty."""
    session = await _run(build_simple_channel(origin, variant_count=2, segment_count=6))
    state = session.sampling_state()

    assert state["segments_sampled"] > 0
    assert state["reason"] == ""
    assert any(entry["total_fetched"] > 0 for entry in state["by_variant"])
    assert all(entry["polls"] > 0 for entry in state["by_variant"])


async def test_a_spent_sample_budget_is_named_rather_than_left_blank(
    origin: FixtureServer,
) -> None:
    url = build_simple_channel(origin, variant_count=2, segment_count=6)
    session = AnalysisSession(
        session_id="test",
        playback_url=url,
        # No budget at all: the session polls playlists and can sample nothing.
        options=SessionOptions(duration_s=4.0, max_segment_samples=0),
    )
    await session.run()
    state = session.sampling_state()

    assert state["segments_sampled"] == 0
    assert "sample budget" in state["reason"]
    assert "0 segment(s)" in state["reason"]


async def test_a_handler_that_crashes_is_reported_as_the_analyzer_s_own_failure(
    origin: FixtureServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detector raising must not read as a stream that produced no segments."""
    from app.analysis import engine as engine_module

    async def explode(self: object, *args: object, **kwargs: object) -> None:
        raise RuntimeError("a detector raised")

    monkeypatch.setattr(engine_module.AnalysisSession, "_on_snapshot", explode)
    session = await _run(build_simple_channel(origin, variant_count=1, segment_count=6))
    state = session.sampling_state()

    assert state["segments_sampled"] == 0
    assert "failed inside the analyzer" in state["reason"]
    assert "a detector raised" in state["reason"]


async def test_a_playlist_that_lists_no_segment_is_named_as_such(
    origin: FixtureServer,
) -> None:
    """The case that needs no exception at all: a playlist with nothing in it."""
    url = build_simple_channel(origin, variant_count=1, segment_count=6)
    empty = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:0\n"
    for path in list(origin.routes):
        if path.endswith(".m3u8") and "master" not in path:
            origin.add_text(path, empty)

    session = await _run(url)
    state = session.sampling_state()

    assert state["segments_sampled"] == 0
    assert "lists no segment" in state["reason"]


# -- the poller no longer swallows -------------------------------------------


async def test_a_failing_snapshot_handler_is_counted_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fetcher = Fetcher()
    target = PollTarget(variant="v720p", url="https://cdn.invalid/x.m3u8")

    async def handler(_: Snapshot) -> None:
        raise ValueError("handler is broken")

    poller = PlaylistPoller(target, fetcher, on_snapshot=handler)
    with caplog.at_level(logging.ERROR):
        poller.start()
        await asyncio.sleep(0.6)
        await poller.stop()
    await fetcher.aclose()

    assert poller.failures >= 1
    assert "handler is broken" in poller.last_failure
    assert "Playlist poll handling failed" in caplog.text


# -- one bad segment costs one segment ---------------------------------------


async def test_a_segment_the_parser_cannot_read_is_measured_and_not_refetched(
    origin: FixtureServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The measurement survives the parser.

    The MSN is recorded as attempted before the body is parsed, so a segment the analyzer
    cannot read is fetched once. It used to be marked afterwards, which meant an unreadable
    body was re-fetched on every poll and the rung never advanced past it.
    """
    from app.analysis.collectors import segment_sampler

    origin.add_bytes("seg.ts", b"not a transport stream", content_type="video/mp2t")

    def refuse(*args: object, **kwargs: object) -> object:
        raise ValueError("the parser raised")

    monkeypatch.setattr(segment_sampler, "analyse", refuse)

    fetcher = Fetcher()
    sampler = SegmentSampler(RungSampling(variant="v720p", full=True), fetcher)
    sample = await sampler.fetch_segment(msn=7, uri=origin.url("seg.ts"), declared_duration=6.0)
    await fetcher.aclose()

    # The transport measurement is intact and the finding text names the analyzer.
    assert sample.result.ok
    assert sample.result.bytes_received == len(b"not a transport stream")
    assert "did not parse" in sample.analysis.parse_error
    # And the rung has moved on.
    assert 7 in sampler.sampling.fetched_msns
    assert sampler.sampling.should_fetch(7) is False
