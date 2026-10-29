"""Findings the analyzer raised against a stream that was fine, because of how it measures.

Each test pairs the case that used to fire on a healthy stream with the defect the rule exists
for, so the fix narrows the rule to the stream and does not silence it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.analysis import vpb
from app.analysis.engine import AnalysisSession, SessionOptions
from app.analysis.rules import media_playlist as media_rules
from app.analysis.rules import segments as segment_rules
from app.analysis.rules import transport as transport_rules
from app.analysis.rules.base import StreamLayer
from app.config import Thresholds
from app.hls.playlist import parse_media
from app.media.segment import SegmentAnalysis
from app.net import dns
from tests.fixtures.server import FixtureServer, build_simple_channel
from tests.fixtures.synth import (
    PlaylistSpec,
    SegmentSpec,
    VariantSpec,
    build_ts_segment,
    render_master_playlist,
    render_media_playlist,
)

T = Thresholds()
LAYER = StreamLayer.PLAYBACK
NOW = dt.datetime(2026, 10, 27, 10, 0, 0, tzinfo=dt.UTC)


def _ids(findings) -> set[str]:  # type: ignore[no-untyped-def]
    if findings is None:
        return set()
    items = findings if isinstance(findings, list) else [findings]
    return {f.rule.id for f in items}


# -- the virtual player buffer -----------------------------------------------------------


def _four_rung_channel(origin: FixtureServer, segment_s: float = 2.0) -> str:
    """Four rungs: the lowest, middle and highest are sampled in full, one every Nth."""
    rungs = [
        ("r1", 600_000, "640x360", 640, 360),
        ("r2", 1_000_000, "960x540", 960, 540),
        ("r3", 1_600_000, "1280x720", 1280, 720),
        ("r4", 2_500_000, "1920x1080", 1920, 1080),
    ]
    origin.add_text(
        "master.m3u8",
        render_master_playlist(
            [
                VariantSpec(name=n, bandwidth=bw, resolution=res, uri=f"{n}.m3u8")
                for n, bw, res, _, _ in rungs
            ]
        ),
    )
    for name, _bw, _res, width, height in rungs:
        spec = PlaylistSpec(
            segment_count=9,
            segment_duration=segment_s,
            target_duration=int(segment_s),
            segment_prefix=f"{name}-seg",
        )
        origin.add_text(f"{name}.m3u8", render_media_playlist(spec))
        for index in range(9):
            origin.add_bytes(
                f"{name}-seg{index}.ts",
                build_ts_segment(
                    SegmentSpec(
                        duration=segment_s,
                        pts_offset_s=index * segment_s,
                        width=width,
                        height=height,
                    )
                ),
            )
    return origin.url("master.m3u8")


async def test_a_rung_sampled_every_nth_segment_is_not_played_back(origin: FixtureServer) -> None:
    session = AnalysisSession(
        session_id="vpb-nth",
        playback_url=_four_rung_channel(origin),
        options=SessionOptions(duration_s=5.0, max_segment_samples=200),
    )
    result = await session.run()
    context = session.layers[LAYER]
    partial = [v for v, s in context.samplers.items() if not s.sampling.full]

    assert partial, "one rung of four is sampled every Nth segment"
    # It is fetched and checked, and never handed to the buffer model.
    assert not set(partial) & set(context.buffers)
    assert not {k for k in result.vpb_results if k in partial}
    on_partial = {f.rule.id for f in result.findings if f.variant in partial}
    assert not on_partial & {"VPB-001", "VPB-002", "VPB-004"}, on_partial


async def test_the_buffer_model_ends_where_the_sample_budget_ran_out(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=4, segment_duration=2.0)
    session = AnalysisSession(
        session_id="vpb-budget",
        playback_url=url,
        # Four segments are all the run may fetch; it then runs on for several seconds.
        options=SessionOptions(duration_s=8.0, max_segment_samples=4),
    )
    result = await session.run()

    assert session._budget_spent_at is not None
    (only,) = result.vpb_results.values()
    # Eight seconds of media were fetched and the model stopped with the fetching; draining
    # it to the end of the run would have called the rest a stall.
    assert only.stall_s == pytest.approx(0.0, abs=0.5)
    assert "VPB-001" not in {f.rule.id for f in result.findings}


def test_the_first_window_fetched_at_once_does_not_overfill_the_buffer() -> None:
    buffer = vpb.VirtualPlayerBuffer("v", target_duration=6.0, thresholds=T)
    for msn in range(6):  # 36 s of window, against a 15 s queue
        buffer.feed(vpb.SegmentDelivery(msn=msn, completed_at=NOW, duration_s=6.0, backlog=True))
    assert buffer.result.buffer_too_long_events == []

    later = vpb.VirtualPlayerBuffer("v", target_duration=6.0, thresholds=T)
    for msn in range(6):  # the same media arriving after the join is an overfilled queue
        later.feed(vpb.SegmentDelivery(msn=msn, completed_at=NOW, duration_s=6.0))
    assert later.result.buffer_too_long_events


# -- MED-015 -------------------------------------------------------------------------------


def test_one_segment_landing_between_two_polls_is_not_faster_than_real_time() -> None:
    # One 6 s segment in the 3 s between two polls, the ordinary refresh.
    assert (
        media_rules.check_publication_rate(
            added_duration_s=6.0,
            elapsed_wall_s=3.0,
            variant="v",
            layer=LAYER,
            target_duration_s=6.0,
        )
        is None
    )
    # 36 s published in 18 s of wall time is twice real time.
    assert "MED-015" in _ids(
        media_rules.check_publication_rate(
            added_duration_s=36.0,
            elapsed_wall_s=18.0,
            variant="v",
            layer=LAYER,
            target_duration_s=6.0,
        )
    )


# -- MED-016 -------------------------------------------------------------------------------


def _signed(msn: int, token: str, path: str = "seg") -> str:
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:6", f"#EXT-X-MEDIA-SEQUENCE:{msn}"]
    for i in range(3):
        lines += ["#EXTINF:6.0,", f"{path}{msn + i}.ts?token={token}"]
    return "\n".join(lines) + "\n"


def test_a_segment_resigned_with_a_new_token_is_the_same_segment() -> None:
    base = "https://cdn.example/v.m3u8"
    previous = parse_media(_signed(10, "a"), base)
    resigned = parse_media(_signed(10, "b"), base)
    moved = parse_media(_signed(10, "a", path="other"), base)
    assert media_rules.check_segment_uri_changed(previous, resigned, variant="v", layer=LAYER) == []
    assert "MED-016" in _ids(
        media_rules.check_segment_uri_changed(previous, moved, variant="v", layer=LAYER)
    )


# -- SEG-005 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("container", "audio_only", "fires"),
    [("webvtt", False, False), ("aac", True, False), ("ts", False, True)],
)
def test_the_tiny_segment_threshold_applies_to_video(
    container: str, audio_only: bool, fires: bool
) -> None:
    analysis = SegmentAnalysis(
        uri="https://cdn.example/s.x",
        container=container,
        byte_size=900,
        msn=1,
        declared_duration=6.0,
    )
    findings = segment_rules.check_segment(
        analysis, variant="v", layer=LAYER, thresholds=T, is_audio_only=audio_only
    )
    assert ("SEG-005" in _ids(findings)) is fires


# -- NET-001 -------------------------------------------------------------------------------


async def test_ipv6_is_not_judged_from_a_host_that_has_no_ipv6_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dns, "host_has_ipv6_route", lambda _address: False)

    async def fake_getaddrinfo(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        import socket

        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 443, 0, 0)),
        ]

    import asyncio

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)

    async def reachable(*_args, **_kwargs) -> bool:  # type: ignore[no-untyped-def]
        return True

    monkeypatch.setattr(dns, "_tcp_reachable", reachable)
    result = await dns.resolve("https://edge.example/master.m3u8")

    assert result.ipv6_reachable is None
    assert "no IPv6 route" in result.ipv6_unmeasured
    assert "NET-001" not in _ids(transport_rules.check_dns(result, layer=LAYER, thresholds=T))

    result.ipv6_reachable = False  # measured from a host that does route IPv6
    assert "NET-001" in _ids(transport_rules.check_dns(result, layer=LAYER, thresholds=T))


# -- SEG-007: the duration a transport stream carries -------------------------------------


def _decode_order(frames: int, step: int = 3000) -> list[int]:
    """PTS values of `frames` frames at 30 fps in decode order with two B-frames: I P B B P B B."""
    order = [0]
    for anchor in range(3, frames - 2, 3):
        order += [anchor * step, (anchor - 2) * step, (anchor - 1) * step]
    # The last two frames: P179 is decoded before B178, which ends the segment's PES order.
    return [*order, (frames - 1) * step, (frames - 2) * step]


def test_a_b_frame_segment_measures_its_full_duration() -> None:
    from app.media.ts import TrackInfo

    track = TrackInfo(pid=256, stream_type=0x1B)
    values = _decode_order(180)  # frames 0..179 shown, each 1/30 s: 6.000 s
    track.first_pts, track.last_pts, track.pts_values = values[0], values[-1], values
    # The last PES in decode order is a B-frame two frames before the end; last-minus-first
    # read 5.933 s and SEG-007 fired on a segment cut exactly at its EXTINF.
    assert (values[-1] - values[0]) / 90000 < 5.95
    assert track.duration_s == pytest.approx(6.0, abs=0.001)


@pytest.mark.parametrize(("actual", "fires"), [(6.0, False), (5.5, True)])
def test_seg_007_compares_the_full_duration_with_extinf(actual: float, fires: bool) -> None:
    analysis = SegmentAnalysis(
        uri="https://cdn.example/s.ts",
        container="ts",
        byte_size=900_000,
        msn=1,
        declared_duration=6.0,
        actual_duration=actual,
    )
    findings = segment_rules.check_segment(analysis, variant="v", layer=LAYER, thresholds=T)
    assert ("SEG-007" in _ids(findings)) is fires
