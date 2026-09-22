"""End-to-end engine runs against the fault-injecting fixture origin.

These are the tests that prove the whole chain works: fetch, parse, evaluate, aggregate,
correlate, attribute, and produce one verdict.
"""

from __future__ import annotations

import asyncio

from app.analysis.engine import AnalysisSession, SessionOptions
from app.analysis.verdict import VerdictStatus
from tests.fixtures.server import (
    FixtureServer,
    Route,
    build_demuxed_channel,
    build_simple_channel,
)
from tests.fixtures.synth import (
    PlaylistSpec,
    SegmentSpec,
    VariantSpec,
    build_ts_segment,
    render_master_playlist,
    render_media_playlist,
)


def rule_ids(result) -> set[str]:  # type: ignore[no-untyped-def]
    return {f.rule.id for f in result.findings}


async def _run(url: str, *, duration: float = 4.0, **kwargs) -> object:  # type: ignore[no-untyped-def]
    session = AnalysisSession(
        session_id="test",
        playback_url=url,
        options=SessionOptions(duration_s=duration, max_segment_samples=60),
        **kwargs,
    )
    return await session.run()


async def test_a_clean_channel_produces_no_stream_side_defect(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=3, segment_count=6)
    result = await _run(url)

    found = rule_ids(result)
    assert "MST-900" in found
    assert "MED-900" in found
    assert "SEG-002" not in found
    assert "SEG-005" not in found
    assert "MED-004" not in found
    assert result.verdict.playlists_checked > 0
    assert result.verdict.segments_checked > 0


async def test_a_404_on_a_listed_segment_is_found_and_attributed_to_the_cdn(
    origin: FixtureServer,
) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=6)
    origin.remove("low-seg2.ts")

    result = await _run(url)
    found = rule_ids(result)
    assert "HTTP-005" in found
    finding = next(f for f in result.findings if f.rule.id == "HTTP-005")
    assert finding.owner.value == "CDN"
    assert "HTTP 404" in finding.detail
    assert result.verdict.status is not VerdictStatus.NO_STREAM_SIDE_DEFECT


async def test_an_under_declared_bandwidth_is_measured_against_the_segments(
    origin: FixtureServer,
) -> None:
    # One rung declaring 50 kbit/s while delivering a much larger segment.
    origin.add_text(
        "master.m3u8",
        render_master_playlist(
            [VariantSpec(name="v", bandwidth=50_000, resolution="1280x720", uri="v.m3u8")]
        ),
    )
    origin.add_text(
        "v.m3u8", render_media_playlist(PlaylistSpec(segment_count=3, segment_prefix="v-seg"))
    )
    for index in range(3):
        origin.add_bytes(
            f"v-seg{index}.ts",
            build_ts_segment(
                SegmentSpec(duration=6.0, pts_offset_s=index * 6.0, padding_bytes=300_000)
            ),
        )

    result = await _run(origin.url("master.m3u8"))
    assert "SEG-015" in rule_ids(result)


async def test_a_stale_playlist_is_found_and_becomes_the_primary_root_cause(
    origin: FixtureServer,
) -> None:
    origin.add_text(
        "master.m3u8",
        render_master_playlist(
            [VariantSpec(name="v", bandwidth=800_000, resolution="640x360", uri="v.m3u8")]
        ),
    )
    # A playlist with a one-second target duration that never advances is stale after 1.5 s.
    origin.add_text(
        "v.m3u8",
        render_media_playlist(
            PlaylistSpec(
                target_duration=1, segment_count=3, segment_duration=1.0, segment_prefix="v-seg"
            )
        ),
    )
    for index in range(3):
        origin.add_bytes(
            f"v-seg{index}.ts",
            build_ts_segment(SegmentSpec(duration=1.0, pts_offset_s=index * 1.0)),
        )

    result = await _run(origin.url("master.m3u8"), duration=8.0)
    found = rule_ids(result)
    assert "MED-004" in found
    stale = next(f for f in result.findings if f.rule.id == "MED-004")
    assert "published no new segment" in stale.detail


async def test_a_tiny_segment_and_a_missing_pat_are_both_reported(origin: FixtureServer) -> None:
    origin.add_text(
        "master.m3u8",
        render_master_playlist(
            [VariantSpec(name="v", bandwidth=800_000, resolution="640x360", uri="v.m3u8")]
        ),
    )
    origin.add_text(
        "v.m3u8", render_media_playlist(PlaylistSpec(segment_count=2, segment_prefix="v-seg"))
    )
    origin.add_bytes("v-seg0.ts", build_ts_segment(SegmentSpec(tiny=True, padding_bytes=0)))
    origin.add_bytes("v-seg1.ts", build_ts_segment(SegmentSpec(missing_pat=True, pts_offset_s=6.0)))

    result = await _run(origin.url("master.m3u8"))
    found = rule_ids(result)
    assert {"SEG-005", "SEG-004"} <= found


async def test_cross_variant_discontinuity_mismatch_is_reported_end_to_end(
    origin: FixtureServer,
) -> None:
    origin.add_text(
        "master.m3u8",
        render_master_playlist(
            [
                VariantSpec(name="low", bandwidth=600_000, resolution="640x360", uri="low.m3u8"),
                VariantSpec(
                    name="high", bandwidth=2_400_000, resolution="1280x720", uri="high.m3u8"
                ),
            ]
        ),
    )
    origin.add_text(
        "low.m3u8",
        render_media_playlist(
            PlaylistSpec(segment_count=4, discontinuity_sequence=3, segment_prefix="low-seg")
        ),
    )
    origin.add_text(
        "high.m3u8",
        render_media_playlist(
            PlaylistSpec(segment_count=4, discontinuity_sequence=9, segment_prefix="high-seg")
        ),
    )
    for prefix in ("low", "high"):
        for index in range(4):
            origin.add_bytes(
                f"{prefix}-seg{index}.ts",
                build_ts_segment(SegmentSpec(duration=6.0, pts_offset_s=index * 6.0)),
            )

    result = await _run(origin.url("master.m3u8"))
    found = rule_ids(result)
    assert "SEQ-009" in found
    mismatch = next(f for f in result.findings if f.rule.id == "SEQ-009")
    assert mismatch.severity.value == "CRITICAL"


async def test_a_redirected_master_resolves_children_against_the_final_url(
    origin: FixtureServer,
) -> None:
    origin.add("entry.m3u8", Route(body=b"", redirect_to="/deep/master.m3u8", redirect_status=307))
    origin.add_text(
        "deep/master.m3u8",
        render_master_playlist(
            [VariantSpec(name="v", bandwidth=800_000, resolution="640x360", uri="v.m3u8")]
        ),
    )
    origin.add_text(
        "deep/v.m3u8",
        render_media_playlist(PlaylistSpec(segment_count=2, segment_prefix="v-seg")),
    )
    for index in range(2):
        origin.add_bytes(
            f"deep/v-seg{index}.ts",
            build_ts_segment(SegmentSpec(duration=6.0, pts_offset_s=index * 6.0)),
        )

    result = await _run(origin.url("entry.m3u8"))
    # The children only exist under /deep/, so any segment finding proves the resolution.
    assert "MST-014" not in rule_ids(result)
    assert result.verdict.segments_checked >= 2
    assert origin.hits("/deep/v-seg0.ts") >= 1


async def test_a_slow_origin_produces_a_download_ratio_finding(origin: FixtureServer) -> None:
    origin.add_text(
        "master.m3u8",
        render_master_playlist(
            [VariantSpec(name="v", bandwidth=800_000, resolution="640x360", uri="v.m3u8")]
        ),
    )
    origin.add_text(
        "v.m3u8",
        render_media_playlist(
            PlaylistSpec(
                target_duration=1, segment_count=2, segment_duration=1.0, segment_prefix="v-seg"
            )
        ),
    )
    for index in range(2):
        origin.add_bytes(
            f"v-seg{index}.ts",
            build_ts_segment(SegmentSpec(duration=1.0, pts_offset_s=index * 1.0)),
            delay_s=1.2,
        )

    result = await _run(origin.url("master.m3u8"), duration=6.0)
    found = rule_ids(result)
    assert {"SEG-017"} & found


async def test_missing_cors_headers_are_reported(origin: FixtureServer) -> None:
    origin.cors_enabled = False
    url = build_simple_channel(origin, variant_count=1, segment_count=3)
    result = await _run(url)
    assert "CDN-005" in rule_ids(result)


async def test_a_session_stops_when_asked_and_still_produces_a_verdict(
    origin: FixtureServer,
) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=4)
    session = AnalysisSession(
        session_id="stoppable",
        playback_url=url,
        options=SessionOptions(duration_s=120.0, max_segment_samples=20),
    )
    task = asyncio.create_task(session.run())
    await asyncio.sleep(2.0)
    session.stop()
    result = await asyncio.wait_for(task, timeout=20)
    assert result.verdict is not None
    assert result.verdict.window_seconds < 120


async def test_a_single_playback_url_records_that_attribution_used_headers(
    origin: FixtureServer,
) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=3)
    result = await _run(url)
    assert "INFO-004" in rule_ids(result)


async def test_the_result_serialises_to_json_safe_types(origin: FixtureServer) -> None:
    import json

    url = build_simple_channel(origin, variant_count=2, segment_count=3)
    result = await _run(url)
    payload = json.dumps(result.as_dict())
    assert len(payload) > 1000
    assert '"verdict"' in payload


# ---------------------------------------------------------------------------
# Demuxed ladders
# ---------------------------------------------------------------------------


async def test_a_demuxed_channel_does_not_trip_the_muxed_audio_rule(
    origin: FixtureServer,
) -> None:
    """The false positive, end to end: a video segment with no audio track is what a demuxed
    rung publishes. `AUD-003` used to fire CRITICAL on every one of them, on every TV Plus
    CMAF channel, and take the verdict with it."""
    url = build_demuxed_channel(origin, variant_count=2, segment_count=6)

    result = await _run(url)

    assert "AUD-003" not in rule_ids(result)
    assert result.verdict.segments_checked > 0


async def test_a_muxed_channel_missing_audio_still_trips_it(origin: FixtureServer) -> None:
    """The alarm the gate must not silence: this rung declares no audio rendition, so its
    own segments are the only place its audio could be, and they carry none."""
    origin.add_text(
        "master.m3u8",
        render_master_playlist([VariantSpec(name="low", bandwidth=600_000, uri="low.m3u8")]),
    )
    spec = PlaylistSpec(segment_count=4, segment_duration=6.0, segment_prefix="low-seg")
    origin.add_text("low.m3u8", render_media_playlist(spec))
    for index in range(4):
        origin.add_bytes(
            f"low-seg{index}.ts",
            build_ts_segment(SegmentSpec(pts_offset_s=index * 6.0, with_audio=False)),
        )

    result = await _run(origin.url("master.m3u8"))

    assert "AUD-003" in rule_ids(result)


async def test_the_ladder_table_states_each_rungs_layout(origin: FixtureServer) -> None:
    url = build_demuxed_channel(origin, variant_count=2, segment_count=4)

    result = await _run(url)

    layouts = {row["variant"]: row["layout"] for row in result.ladder}
    assert layouts
    for entry in layouts.values():
        assert entry is not None
        assert entry["layout"] == "demuxed"
        assert entry["audio_variants"]


async def test_skew_between_a_video_rung_and_its_audio_rendition_is_measured(
    origin: FixtureServer,
) -> None:
    """`AV-001` to `AV-005` never ran on a demuxed ladder: they read a skew that only a
    segment carrying both tracks can produce. Here the audio rendition starts 400 ms after
    the video it is presented with, and no single segment holds both."""
    url = build_demuxed_channel(origin, variant_count=1, segment_count=6, audio_pts_offset_ms=400.0)

    result = await _run(url, duration=6.0)

    skew = [f for f in result.findings if f.rule.id == "AV-001"]
    assert skew, "cross-rendition skew produced no finding"
    evidence = skew[0].evidence[0]
    assert evidence["audio_variant"] != evidence["video_variant"]
    assert round(evidence["av_skew_ms"]) == 400
