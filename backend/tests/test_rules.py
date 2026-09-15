"""Rule detectors, each with a positive and a negative case.

Every test drives the detector with a measurement that either does or does not breach the
threshold, so a rule that stops firing — or starts firing on a clean stream — fails here.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from app.analysis.rules import ads as ads_rules
from app.analysis.rules import master as master_rules
from app.analysis.rules import media_playlist as media_rules
from app.analysis.rules import segments as segment_rules
from app.analysis.rules import sequence as sequence_rules
from app.analysis.rules import subtitles as subtitle_rules
from app.analysis.rules import transport as transport_rules
from app.analysis.rules.base import Severity, StreamLayer
from app.config import Thresholds
from app.hls.playlist import MasterPlaylist, parse_master, parse_media
from app.media.segment import analyse as analyse_segment
from app.net.dns import DnsResult
from app.net.fetcher import FetchResult, Timings
from app.net.tls_inspect import TlsResult
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
BASE = "https://cdn.example/live/ch1/720p.m3u8"
NOW = dt.datetime(2026, 9, 11, 10, 0, 0, tzinfo=dt.UTC)


def ids(findings) -> set[str]:  # type: ignore[no-untyped-def]
    return {f.rule.id for f in findings}


# ---------------------------------------------------------------------------
# DNS and TLS
# ---------------------------------------------------------------------------


def test_aaaa_without_ipv6_transit_fires_and_a_reachable_pair_does_not() -> None:
    broken = DnsResult(
        host="cdn.example",
        a_records=["1.2.3.4"],
        aaaa_records=["2001:db8::1"],
        ipv4_reachable=True,
        ipv6_reachable=False,
        resolve_ms=10.0,
    )
    healthy = DnsResult(
        host="cdn.example",
        a_records=["1.2.3.4"],
        aaaa_records=["2001:db8::1"],
        ipv4_reachable=True,
        ipv6_reachable=True,
        resolve_ms=10.0,
    )
    assert "NET-001" in ids(transport_rules.check_dns(broken, layer=LAYER, thresholds=T))
    assert "NET-001" not in ids(transport_rules.check_dns(healthy, layer=LAYER, thresholds=T))
    assert "NET-900" in ids(transport_rules.check_dns(healthy, layer=LAYER, thresholds=T))


def test_incomplete_tls_chain_fires_and_a_complete_one_passes() -> None:
    broken = TlsResult(
        host="cdn.example",
        port=443,
        negotiated_version="TLSv1.2",
        negotiated_cipher="ECDHE-RSA-AES128-GCM-SHA256",
        chain_complete=False,
        chain_length=1,
        san=["cdn.example"],
        san_matches_host=True,
        days_to_expiry=100,
        legacy_cipher_offered={"ECDHE-RSA-AES128-SHA": True},
    )
    good = TlsResult(
        host="cdn.example",
        port=443,
        negotiated_version="TLSv1.2",
        negotiated_cipher="ECDHE-RSA-AES128-GCM-SHA256",
        chain_complete=True,
        chain_length=3,
        san=["cdn.example"],
        san_matches_host=True,
        days_to_expiry=100,
        legacy_cipher_offered={"ECDHE-RSA-AES128-SHA": True},
    )
    assert "TLS-001" in ids(transport_rules.check_tls(broken, layer=LAYER))
    assert ids(transport_rules.check_tls(good, layer=LAYER)) == {"TLS-900"}


def test_no_legacy_cipher_available_is_reported() -> None:
    result = TlsResult(
        host="cdn.example",
        port=443,
        negotiated_version="TLSv1.3",
        negotiated_cipher="TLS_AES_128_GCM_SHA256",
        chain_complete=True,
        san=["cdn.example"],
        san_matches_host=True,
        days_to_expiry=90,
        legacy_cipher_offered={"ECDHE-RSA-AES128-SHA": False, "AES128-SHA": False},
    )
    assert "TLS-004" in ids(transport_rules.check_tls(result, layer=LAYER))


# ---------------------------------------------------------------------------
# Master playlist
# ---------------------------------------------------------------------------


def test_missing_codecs_resolution_and_framerate_each_fire() -> None:
    text = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-STREAM-INF:BANDWIDTH=1200000\nv.m3u8\n"
    found = ids(master_rules.check_master(parse_master(text, BASE), layer=LAYER, thresholds=T))
    assert {"MST-003", "MST-004", "MST-005"} <= found


def test_a_complete_master_passes() -> None:
    text = render_master_playlist(
        [
            VariantSpec(name="low", bandwidth=600_000, resolution="640x360"),
            VariantSpec(name="mid", bandwidth=1_200_000, resolution="1280x720"),
        ]
    )
    found = ids(master_rules.check_master(parse_master(text, BASE), layer=LAYER, thresholds=T))
    assert "MST-900" in found
    assert not {"MST-003", "MST-004", "MST-005", "MST-006"} & found


def test_a_high_first_rung_is_flagged_as_the_tizen_startup_rung() -> None:
    high_first = render_master_playlist(
        [
            VariantSpec(name="high", bandwidth=3_500_000, resolution="1920x1080"),
            VariantSpec(name="low", bandwidth=600_000, resolution="640x360"),
        ]
    )
    low_first = render_master_playlist(
        [
            VariantSpec(name="low", bandwidth=600_000, resolution="640x360"),
            VariantSpec(name="high", bandwidth=3_500_000, resolution="1920x1080"),
        ]
    )
    assert "MST-010" in ids(
        master_rules.check_master(parse_master(high_first, BASE), layer=LAYER, thresholds=T)
    )
    assert "MST-010" not in ids(
        master_rules.check_master(parse_master(low_first, BASE), layer=LAYER, thresholds=T)
    )


def test_a_referenced_audio_group_that_is_not_defined_fires() -> None:
    text = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1280x720,FRAME-RATE=30,"
        'CODECS="avc1.64001f,mp4a.40.2",AUDIO="aac-missing"\n'
        "v.m3u8\n"
    )
    assert "MST-016" in ids(
        master_rules.check_master(parse_master(text, BASE), layer=LAYER, thresholds=T)
    )


def test_mixed_muxed_and_demuxed_variants_fire() -> None:
    text = (
        "#EXTM3U\n"
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a1",NAME="English",LANGUAGE="en",URI="a.m3u8"\n'
        "#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1280x720,FRAME-RATE=30,"
        'CODECS="avc1.64001f",AUDIO="a1"\n'
        "v1.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1920x1080,FRAME-RATE=30,"
        'CODECS="avc1.64001f,mp4a.40.2"\n'
        "v2.m3u8\n"
    )
    assert "MST-015" in ids(
        master_rules.check_master(parse_master(text, BASE), layer=LAYER, thresholds=T)
    )


def test_a_master_that_changes_between_polls_fires() -> None:
    before = parse_master(
        render_master_playlist([VariantSpec(name="a", bandwidth=1_000_000)]), BASE
    )
    after = parse_master(
        render_master_playlist(
            [
                VariantSpec(name="a", bandwidth=1_000_000),
                VariantSpec(name="b", bandwidth=2_000_000),
            ]
        ),
        BASE,
    )
    assert "MST-020" in ids(master_rules.check_master_changed(before, after, layer=LAYER))
    assert master_rules.check_master_changed(before, before, layer=LAYER) == []


def _ladder(session: str, **overrides: Any) -> MasterPlaylist:
    """The same ladder served under one ad-inserter session, as MediaTailor serves it."""
    rungs = [("low", 600_000, "640x360"), ("high", 2_000_000, "1920x1080")]
    specs = [
        VariantSpec(
            name=name,
            bandwidth=bandwidth,
            resolution=resolution,
            uri=f"v1/session/{session}/{name}.m3u8",
            **overrides,
        )
        for name, bandwidth, resolution in rungs
    ]
    return parse_master(render_master_playlist(specs), BASE)


def test_a_master_whose_only_change_is_the_ad_inserter_session_does_not_fire() -> None:
    """MediaTailor mints a session per master fetch, so every child URI changes each poll.

    The ladder is identical; only the session path moved. Keying the comparison on the URI
    reported the whole ladder as replaced every 20 s, which is the false positive this
    guards against.
    """
    before = _ladder("2f6d1c40-0a11-4f0e-9a52-7c1b0e5f9a01")
    after = _ladder("9b8e7d60-5c22-41aa-8e37-1d4f0a2c3b55")

    assert before.variants[0].resolved_uri != after.variants[0].resolved_uri
    assert master_rules.check_master_changed(before, after, layer=LAYER) == []


@pytest.mark.parametrize(
    ("attribute", "override"),
    [
        ("CODECS", {"codecs": "hvc1.2.4.L120.B0,mp4a.40.2"}),
        ("FRAME-RATE", {"frame_rate": 50.0}),
    ],
)
def test_a_real_ladder_change_still_fires_through_a_session_rotation(
    attribute: str, override: dict[str, Any]
) -> None:
    """A codec or frame-rate rewrite is reported even though the session moved too."""
    before = _ladder("2f6d1c40-0a11-4f0e-9a52-7c1b0e5f9a01")
    after = _ladder("9b8e7d60-5c22-41aa-8e37-1d4f0a2c3b55", **override)

    findings = master_rules.check_master_changed(before, after, layer=LAYER)
    assert "MST-020" in ids(findings)
    assert attribute in findings[0].detail
    assert attribute in findings[0].evidence[0]["rewritten"]["v1080p@2000k"]


def test_a_rung_added_under_a_new_session_is_reported_as_an_added_rung() -> None:
    before = _ladder("2f6d1c40-0a11-4f0e-9a52-7c1b0e5f9a01")
    after = parse_master(
        render_master_playlist(
            [
                VariantSpec(
                    name="low", bandwidth=600_000, resolution="640x360", uri="v1/session/b/low.m3u8"
                ),
                VariantSpec(
                    name="mid",
                    bandwidth=1_200_000,
                    resolution="1280x720",
                    uri="v1/session/b/mid.m3u8",
                ),
                VariantSpec(
                    name="high",
                    bandwidth=2_000_000,
                    resolution="1920x1080",
                    uri="v1/session/b/high.m3u8",
                ),
            ]
        ),
        BASE,
    )
    findings = master_rules.check_master_changed(before, after, layer=LAYER)
    assert "MST-020" in ids(findings)
    assert findings[0].evidence[0]["added"] == ["v720p@1200k"]


def _rated(bandwidth: int, average: int | None = None) -> MasterPlaylist:
    """One rung whose declared rates are the only thing that moves between polls."""
    return parse_master(
        render_master_playlist(
            [
                VariantSpec(
                    name="v",
                    bandwidth=bandwidth,
                    average_bandwidth=average,
                    resolution="1920x1080",
                    uri="v1/high.m3u8",
                )
            ]
        ),
        BASE,
    )


def test_a_bandwidth_move_past_the_tolerance_fires_its_own_rule_and_not_the_ladder_rule() -> None:
    """A rate change is a rate change, reported as `Bandwidth variation`, never as MST-020."""
    findings = master_rules.check_master_changed(
        _rated(1_000_000), _rated(1_100_000), layer=LAYER, thresholds=T
    )
    assert ids(findings) == {"MST-029"}
    assert "1000000" in findings[0].detail and "1100000" in findings[0].detail
    assert "10.0%" in findings[0].detail
    assert findings[0].evidence[0]["rungs"]["v1080p@1100k"]["BANDWIDTH"]["delta"] == 0.1


def test_a_bandwidth_move_inside_the_tolerance_fires_nothing() -> None:
    """A packager recomputes the rate every poll; 4 % is that recomputation, not a defect."""
    assert (
        master_rules.check_master_changed(
            _rated(1_000_000), _rated(1_040_000), layer=LAYER, thresholds=T
        )
        == []
    )


def test_an_average_bandwidth_move_past_the_tolerance_fires() -> None:
    findings = master_rules.check_master_changed(
        _rated(1_000_000, 800_000), _rated(1_000_000, 900_000), layer=LAYER, thresholds=T
    )
    assert ids(findings) == {"MST-029"}
    assert "AVERAGE-BANDWIDTH" in findings[0].detail


def test_declared_codecs_against_a_measured_sps_fires_on_a_mismatch() -> None:
    master = parse_master(
        render_master_playlist(
            [
                VariantSpec(
                    name="v", bandwidth=1_200_000, resolution="1920x1080", codecs="hvc1.1.6.L93.B0"
                )
            ]
        ),
        BASE,
    )
    variant = master.variants[0]
    measured = {"sps": {"codec": "h264", "resolution": "1280x720", "codec_string": "avc1.64001f"}}
    found = ids(master_rules.check_cross_level(variant=variant, measured=measured, layer=LAYER))
    assert {"MST-009", "MST-025"} <= found

    aligned = {"sps": {"codec": "h264", "resolution": "1920x1080", "codec_string": "avc1.64001f"}}
    master2 = parse_master(
        render_master_playlist(
            [
                VariantSpec(
                    name="v", bandwidth=1_200_000, resolution="1920x1080", codecs="avc1.64001f"
                )
            ]
        ),
        BASE,
    )
    assert (
        master_rules.check_cross_level(variant=master2.variants[0], measured=aligned, layer=LAYER)
        == []
    )


# ---------------------------------------------------------------------------
# Media playlist
# ---------------------------------------------------------------------------


def test_extinf_above_target_duration_fires_and_a_conformant_playlist_does_not() -> None:
    bad = parse_media(
        render_media_playlist(
            PlaylistSpec(target_duration=6, segment_count=3, durations=[6.0, 9.5, 6.0])
        ),
        BASE,
    )
    good = parse_media(
        render_media_playlist(PlaylistSpec(target_duration=6, segment_count=6)), BASE
    )
    bad_ids = ids(media_rules.check_media_playlist(bad, variant="720p", layer=LAYER, thresholds=T))
    good_ids = ids(
        media_rules.check_media_playlist(good, variant="720p", layer=LAYER, thresholds=T)
    )
    assert {"MED-001", "MED-005"} <= bad_ids
    assert not {"MED-001", "MED-005"} & good_ids
    assert "MED-900" in good_ids


def test_a_short_live_window_fires() -> None:
    short = parse_media(
        render_media_playlist(PlaylistSpec(target_duration=6, segment_count=2)), BASE
    )
    assert "MED-003" in ids(
        media_rules.check_media_playlist(short, variant="720p", layer=LAYER, thresholds=T)
    )


def test_a_negative_or_unparseable_duration_fires() -> None:
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:-1,\ns0.ts\n"
    assert "MED-007" in ids(
        media_rules.check_media_playlist(
            parse_media(text, BASE), variant="720p", layer=LAYER, thresholds=T
        )
    )


def test_an_ext_x_gap_tag_fires() -> None:
    playlist = parse_media(render_media_playlist(PlaylistSpec(segment_count=6, gap_at={2})), BASE)
    assert "MED-012" in ids(
        media_rules.check_media_playlist(playlist, variant="720p", layer=LAYER, thresholds=T)
    )


def test_a_stale_playlist_fires_after_the_stale_window_and_not_before() -> None:
    machine = media_rules.PlaylistStateMachine(variant="720p")
    playlist = parse_media(render_media_playlist(PlaylistSpec(segment_count=6)), BASE)
    machine.observe(at=NOW, http_ok=True, playlist=playlist, stale_after_s=9.0)

    fresh = media_rules.check_freshness(
        machine=machine,
        at=NOW + dt.timedelta(seconds=5),
        target_duration=6.0,
        variant="720p",
        layer=LAYER,
        thresholds=T,
    )
    stale = media_rules.check_freshness(
        machine=machine,
        at=NOW + dt.timedelta(seconds=20),
        target_duration=6.0,
        variant="720p",
        layer=LAYER,
        thresholds=T,
    )
    assert fresh is None
    assert stale is not None
    assert stale.rule.id == "MED-004"
    assert "20.0 s" in stale.detail


def test_the_playlist_state_machine_walks_live_then_stalled_then_live_end() -> None:
    machine = media_rules.PlaylistStateMachine(variant="720p")
    first = parse_media(
        render_media_playlist(PlaylistSpec(media_sequence=10, segment_count=4)), BASE
    )
    machine.observe(at=NOW, http_ok=True, playlist=first, stale_after_s=9.0)
    assert machine.state is media_rules.PlaylistState.LIVE

    machine.observe(
        at=NOW + dt.timedelta(seconds=15), http_ok=True, playlist=first, stale_after_s=9.0
    )
    assert machine.state is media_rules.PlaylistState.STALLED

    ended = parse_media(
        render_media_playlist(PlaylistSpec(media_sequence=10, segment_count=4, endlist=True)), BASE
    )
    transition = machine.observe(
        at=NOW + dt.timedelta(seconds=20), http_ok=True, playlist=ended, stale_after_s=9.0
    )
    assert machine.state is media_rules.PlaylistState.LIVE_END
    assert transition is not None
    finding = media_rules.check_state_transition(transition, variant="720p", layer=LAYER)
    assert finding is not None and finding.rule.id == "MED-008"


def _fetch(
    status: int, *, body: bytes = b"#EXTM3U\n", error: str | None = None, transport: bool = False
) -> FetchResult:
    return FetchResult(
        requested_url=BASE,
        final_url=BASE,
        status=status,
        headers={},
        body=body,
        timings=Timings(ttfb_ms=42.0, total_ms=50.0),
        error=error,
        transport_error=transport,
    )


def _http_error_transition(
    result: FetchResult, *, parse_error: str = ""
) -> media_rules.StateTransition:
    """Drive one playlist from LIVE into HTTP_ERROR and hand back the transition."""
    machine = media_rules.PlaylistStateMachine(variant="v1080p@6852k")
    live = parse_media(render_media_playlist(PlaylistSpec(segment_count=6)), BASE)
    machine.observe(at=NOW, http_ok=True, playlist=live, stale_after_s=9.0)
    failure = media_rules.describe_fetch_failure(result, None, parse_error=parse_error)
    transition = machine.observe(
        at=NOW + dt.timedelta(seconds=3),
        http_ok=result.ok,
        playlist=None,
        stale_after_s=9.0,
        failure=failure,
    )
    assert transition is not None
    return transition


def test_a_failed_playlist_download_names_the_status_code_the_origin_returned() -> None:
    """The operator asked for the status. It is in the detail and in the evidence."""
    transition = _http_error_transition(_fetch(404, body=b""))
    finding = media_rules.check_state_transition(transition, variant="v1080p@6852k", layer=LAYER)
    assert finding is not None and finding.rule.id == "MED-021"
    assert "HTTP 404 Not Found" in finding.detail
    assert finding.evidence[0]["http_status"] == 404
    assert finding.evidence[0]["ttfb_ms"] == 42.0


def test_a_failed_playlist_download_names_the_transport_error_when_no_response_arrived() -> None:
    transition = _http_error_transition(
        _fetch(0, body=b"", error="ConnectTimeout: timed out", transport=True)
    )
    finding = media_rules.check_state_transition(transition, variant="v1080p@6852k", layer=LAYER)
    assert finding is not None and finding.rule.id == "MED-021"
    assert "ConnectTimeout: timed out" in finding.detail
    assert finding.evidence[0]["http_status"] == 0


def test_a_two_hundred_with_an_empty_body_is_reported_as_an_empty_body() -> None:
    transition = _http_error_transition(_fetch(200, body=b""))
    finding = media_rules.check_state_transition(transition, variant="v1080p@6852k", layer=LAYER)
    assert finding is not None and "empty body" in finding.detail


def test_a_parser_error_on_a_delivered_body_raises_no_finding_against_the_stream() -> None:
    """The analyzer's own defect. It is logged where the poll happens, never charged here."""
    transition = _http_error_transition(
        _fetch(200, body=b"#EXTM3U\n#EXTINF:6,\ns0.ts\n"),
        parse_error="ValueError: invalid literal for int()",
    )
    assert transition.failure is not None and transition.failure.origin_fault is False
    assert media_rules.check_state_transition(transition, variant="v", layer=LAYER) is None


def test_publication_faster_than_real_time_fires() -> None:
    too_fast = media_rules.check_publication_rate(
        added_duration_s=30.0, elapsed_wall_s=5.0, variant="720p", layer=LAYER
    )
    normal = media_rules.check_publication_rate(
        added_duration_s=6.0, elapsed_wall_s=6.0, variant="720p", layer=LAYER
    )
    assert too_fast is not None and too_fast.rule.id == "MED-015"
    assert normal is None


def test_a_stuck_subtitle_playlist_fires() -> None:
    before = parse_media(
        render_media_playlist(
            PlaylistSpec(media_sequence=100, segment_count=3, segment_prefix="web")
        ),
        BASE,
    )
    after_text = (
        render_media_playlist(
            PlaylistSpec(media_sequence=103, segment_count=3, segment_prefix="web")
        )
        .replace("web103.ts", "web100.ts")
        .replace("web104.ts", "web101.ts")
        .replace("web105.ts", "web102.ts")
    )
    after = parse_media(after_text, BASE)
    finding = media_rules.check_subtitle_stuck(before, after, variant="sub_en", layer=LAYER)
    assert finding is not None and finding.rule.id == "MED-013"


def test_a_segment_uri_that_changes_for_the_same_msn_fires() -> None:
    before = parse_media(
        render_media_playlist(PlaylistSpec(media_sequence=50, segment_count=3, segment_prefix="a")),
        BASE,
    )
    after = parse_media(
        render_media_playlist(PlaylistSpec(media_sequence=50, segment_count=3, segment_prefix="b")),
        BASE,
    )
    findings = media_rules.check_segment_uri_changed(before, after, variant="720p", layer=LAYER)
    assert ids(findings) == {"MED-016"}


# ---------------------------------------------------------------------------
# Sequence numbers
# ---------------------------------------------------------------------------


def test_msn_increment_mismatch_fires_and_a_correct_roll_does_not() -> None:
    before = parse_media(
        render_media_playlist(PlaylistSpec(media_sequence=100, segment_count=5)), BASE
    )
    correct = parse_media(
        render_media_playlist(PlaylistSpec(media_sequence=101, segment_count=5)), BASE
    )
    # The sequence number advances by three while only one segment left the window.
    wrong_text = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:103\n"
        + "".join(f"#EXTINF:6.000,\nseg{n}.ts\n" for n in range(101, 106))
    )
    wrong = parse_media(wrong_text, BASE)
    assert "SEQ-001" not in ids(
        sequence_rules.check_sequence_transition(before, correct, variant="720p", layer=LAYER)
    )
    assert "SEQ-001" in ids(
        sequence_rules.check_sequence_transition(before, wrong, variant="720p", layer=LAYER)
    )


def test_a_backwards_msn_fires_and_names_the_cdn_when_headers_prove_a_cache() -> None:
    before = parse_media(
        render_media_playlist(PlaylistSpec(media_sequence=200, segment_count=5)), BASE
    )
    after = parse_media(
        render_media_playlist(PlaylistSpec(media_sequence=195, segment_count=5)), BASE
    )
    found = ids(
        sequence_rules.check_sequence_transition(
            before, after, variant="720p", layer=LAYER, cdn_headers={"x-cache": "HIT", "age": "18"}
        )
    )
    assert {"SEQ-002", "CDN-008"} <= found


def test_a_large_msn_jump_fires() -> None:
    before = parse_media(
        render_media_playlist(PlaylistSpec(media_sequence=100, segment_count=4)), BASE
    )
    after = parse_media(
        render_media_playlist(PlaylistSpec(media_sequence=140, segment_count=4)), BASE
    )
    assert "SEQ-003" in ids(
        sequence_rules.check_sequence_transition(before, after, variant="720p", layer=LAYER)
    )


def test_a_discontinuity_sequence_jump_above_one_fires() -> None:
    before = parse_media(
        render_media_playlist(
            PlaylistSpec(media_sequence=10, segment_count=4, discontinuity_sequence=3)
        ),
        BASE,
    )
    after = parse_media(
        render_media_playlist(
            PlaylistSpec(media_sequence=11, segment_count=4, discontinuity_sequence=6)
        ),
        BASE,
    )
    assert "SEQ-007" in ids(
        sequence_rules.check_sequence_transition(before, after, variant="720p", layer=LAYER)
    )


def _snapshot(
    variant: str, spec: PlaylistSpec, kind: str = "video"
) -> sequence_rules.VariantSnapshot:
    return sequence_rules.VariantSnapshot(
        variant=variant, playlist=parse_media(render_media_playlist(spec), BASE), at=NOW, kind=kind
    )


def test_cross_variant_msn_spread_fires_at_the_tolerance_and_not_below() -> None:
    aligned = [
        _snapshot("low", PlaylistSpec(media_sequence=100, segment_count=5)),
        _snapshot("high", PlaylistSpec(media_sequence=102, segment_count=5)),
    ]
    spread = [
        _snapshot("low", PlaylistSpec(media_sequence=100, segment_count=5)),
        _snapshot("high", PlaylistSpec(media_sequence=110, segment_count=5)),
    ]
    assert "SEQ-008" not in ids(
        sequence_rules.check_cross_variant(aligned, layer=LAYER, thresholds=T, at=NOW)
    )
    assert "SEQ-008" in ids(
        sequence_rules.check_cross_variant(spread, layer=LAYER, thresholds=T, at=NOW)
    )


def test_cross_variant_discontinuity_mismatch_is_critical_for_tizen() -> None:
    snapshots = [
        _snapshot(
            "low", PlaylistSpec(media_sequence=100, segment_count=5, discontinuity_sequence=4)
        ),
        _snapshot(
            "high", PlaylistSpec(media_sequence=100, segment_count=5, discontinuity_sequence=5)
        ),
    ]
    findings = sequence_rules.check_cross_variant(snapshots, layer=LAYER, thresholds=T, at=NOW)
    mismatch = next(f for f in findings if f.rule.id == "SEQ-009")
    assert mismatch.severity is Severity.CRITICAL
    assert "one counter" in mismatch.detail


def test_audio_and_video_declaring_different_discontinuity_counts_fires() -> None:
    snapshots = [
        _snapshot(
            "v720", PlaylistSpec(media_sequence=100, segment_count=5, discontinuity_sequence=4)
        ),
        _snapshot(
            "audio_en",
            PlaylistSpec(media_sequence=100, segment_count=5, discontinuity_sequence=7),
            kind="audio",
        ),
    ]
    assert "AV-003" in ids(
        sequence_rules.check_cross_variant(snapshots, layer=LAYER, thresholds=T, at=NOW)
    )


def _skewed_ladder() -> list[sequence_rules.VariantSnapshot]:
    """One rung polled a beat after the other, across a discontinuity at segment 100.

    This is the shape an operator reported as a false positive. The laggard still lists
    segment 99 and tags 100 as discontinuous; the leader's window has slid to 100, so the
    tag is gone and the count has moved into EXT-X-DISCONTINUITY-SEQUENCE. The declared
    headers read 0 and 1 and the tag positions read [100] and [], yet both rungs agree on
    every segment they both publish.
    """
    return [
        _snapshot(
            "v360p@1436k",
            PlaylistSpec(media_sequence=99, segment_count=6, discontinuity_at={1}),
        ),
        _snapshot(
            "v1080p@8566k",
            PlaylistSpec(media_sequence=100, segment_count=6, discontinuity_sequence=1),
        ),
    ]


def test_poll_skew_across_a_discontinuity_fires_neither_cross_variant_rule() -> None:
    found = ids(
        sequence_rules.check_cross_variant(_skewed_ladder(), layer=LAYER, thresholds=T, at=NOW)
    )
    assert "SEQ-009" not in found
    assert "SEQ-011" not in found


def test_a_dsn_spread_beyond_the_tolerance_fires_whatever_the_windows_say() -> None:
    """No ordering of polls moves one rung three discontinuities ahead of another."""
    snapshots = [
        _snapshot(
            "low", PlaylistSpec(media_sequence=100, segment_count=5, discontinuity_sequence=1)
        ),
        _snapshot(
            "high", PlaylistSpec(media_sequence=100, segment_count=5, discontinuity_sequence=5)
        ),
    ]
    finding = next(
        f
        for f in sequence_rules.check_cross_variant(snapshots, layer=LAYER, thresholds=T, at=NOW)
        if f.rule.id == "SEQ-009"
    )
    assert "span 4" in finding.detail
    assert finding.evidence[0]["spread"] == 4
    assert finding.evidence[0]["tolerance"] == T.cross_variant_dsn_tolerance


def test_a_dsn_disagreement_at_a_segment_every_rung_lists_fires_inside_the_tolerance() -> None:
    """Same segment, different discontinuity count: no poll ordering explains that."""
    snapshots = [
        _snapshot(
            "low", PlaylistSpec(media_sequence=100, segment_count=5, discontinuity_sequence=4)
        ),
        _snapshot(
            "high", PlaylistSpec(media_sequence=100, segment_count=5, discontinuity_sequence=5)
        ),
    ]
    finding = next(
        f
        for f in sequence_rules.check_cross_variant(snapshots, layer=LAYER, thresholds=T, at=NOW)
        if f.rule.id == "SEQ-009"
    )
    assert "Media sequence 101" in finding.detail
    assert finding.evidence[0]["anchor_msn"] == 101


def test_the_audio_video_discontinuity_split_follows_the_ladder_judgement() -> None:
    """AV-003 described the same poll skew from another angle; it is gated on the same test."""
    skew = _skewed_ladder()
    skew[0].kind = "audio"
    assert "AV-003" not in ids(
        sequence_rules.check_cross_variant(skew, layer=LAYER, thresholds=T, at=NOW)
    )


def test_a_tag_only_one_rung_carries_inside_the_common_window_still_fires() -> None:
    """The real defect: both rungs publish segment 102, one calls it discontinuous."""
    snapshots = [
        _snapshot("low", PlaylistSpec(media_sequence=100, segment_count=6, discontinuity_at={2})),
        _snapshot("high", PlaylistSpec(media_sequence=100, segment_count=6)),
    ]
    finding = next(
        f
        for f in sequence_rules.check_cross_variant(snapshots, layer=LAYER, thresholds=T, at=NOW)
        if f.rule.id == "SEQ-011"
    )
    assert "101-105" in finding.detail
    assert finding.evidence[0]["positions"]["low"] == [102]
    assert finding.evidence[0]["positions"]["high"] == []


def test_discontinuity_tags_at_different_positions_fire() -> None:
    snapshots = [
        _snapshot("low", PlaylistSpec(media_sequence=100, segment_count=6, discontinuity_at={2})),
        _snapshot("high", PlaylistSpec(media_sequence=100, segment_count=6, discontinuity_at={4})),
    ]
    assert "SEQ-011" in ids(
        sequence_rules.check_cross_variant(snapshots, layer=LAYER, thresholds=T, at=NOW)
    )


# ---------------------------------------------------------------------------
# Segments and bitstreams
# ---------------------------------------------------------------------------


def _analysed(spec: SegmentSpec, *, declared: float = 6.0, msn: int = 1):  # type: ignore[no-untyped-def]
    return analyse_segment(
        build_ts_segment(spec), uri=f"seg{msn}.ts", declared_duration=declared, msn=msn
    )


def test_a_tiny_segment_fires_and_a_full_one_does_not() -> None:
    tiny = _analysed(SegmentSpec(tiny=True, padding_bytes=0))
    full = _analysed(SegmentSpec())
    assert "SEG-005" in ids(
        segment_rules.check_segment(tiny, variant="720p", layer=LAYER, thresholds=T)
    )
    assert "SEG-005" not in ids(
        segment_rules.check_segment(full, variant="720p", layer=LAYER, thresholds=T)
    )


def test_a_missing_pat_fires() -> None:
    analysis = _analysed(SegmentSpec(missing_pat=True))
    assert "SEG-004" in ids(
        segment_rules.check_segment(analysis, variant="720p", layer=LAYER, thresholds=T)
    )


def test_a_broken_sync_byte_fires() -> None:
    analysis = _analysed(SegmentSpec(broken_sync=True))
    assert "SEG-002" in ids(
        segment_rules.check_segment(analysis, variant="720p", layer=LAYER, thresholds=T)
    )


def test_a_duration_mismatch_against_extinf_fires() -> None:
    analysis = _analysed(SegmentSpec(duration=6.0), declared=4.0)
    found = ids(segment_rules.check_segment(analysis, variant="720p", layer=LAYER, thresholds=T))
    assert "SEG-007" in found


def test_a_segment_that_does_not_start_on_an_idr_fires() -> None:
    analysis = _analysed(SegmentSpec(start_with_idr=False, include_sps=False))
    found = ids(segment_rules.check_segment(analysis, variant="720p", layer=LAYER, thresholds=T))
    assert {"VID-001", "VID-002"} <= found


def test_a_segment_bitrate_above_the_declared_bandwidth_fires() -> None:
    analysis = _analysed(SegmentSpec(duration=6.0, padding_bytes=400_000))
    found = ids(
        segment_rules.check_segment(
            analysis, variant="720p", layer=LAYER, thresholds=T, declared_bandwidth=100_000
        )
    )
    assert "SEG-015" in found
    clean = ids(
        segment_rules.check_segment(
            analysis, variant="720p", layer=LAYER, thresholds=T, declared_bandwidth=10_000_000
        )
    )
    assert "SEG-015" not in clean


def test_a_pts_gap_without_a_discontinuity_fires_and_is_silent_when_declared() -> None:
    history = segment_rules.RungHistory(variant="720p")
    first = _analysed(SegmentSpec(duration=6.0, pts_offset_s=0.0), msn=1)
    segment_rules.check_segment_pair(
        history, first, layer=LAYER, thresholds=T, discontinuity_before=False
    )
    # Second segment starts 3 s after the first one ends.
    second = _analysed(SegmentSpec(duration=6.0, pts_offset_s=9.0), msn=2)

    undeclared = segment_rules.check_segment_pair(
        segment_rules.RungHistory(variant="720p", last=first),
        second,
        layer=LAYER,
        thresholds=T,
        discontinuity_before=False,
    )
    declared = segment_rules.check_segment_pair(
        segment_rules.RungHistory(variant="720p", last=first),
        second,
        layer=LAYER,
        thresholds=T,
        discontinuity_before=True,
    )
    assert "SEG-008" in ids(undeclared)
    assert "SEG-008" not in ids(declared)


def test_a_pts_reset_without_a_discontinuity_is_critical() -> None:
    first = _analysed(SegmentSpec(duration=6.0, pts_offset_s=100.0), msn=1)
    second = _analysed(SegmentSpec(duration=6.0, pts_offset_s=0.0), msn=2)
    findings = segment_rules.check_segment_pair(
        segment_rules.RungHistory(variant="720p", last=first),
        second,
        layer=LAYER,
        thresholds=T,
        discontinuity_before=False,
    )
    reset = next(f for f in findings if f.rule.id == "SEG-010")
    assert reset.severity is Severity.CRITICAL


def test_an_aac_configuration_change_between_segments_is_critical() -> None:
    first = _analysed(SegmentSpec(duration=6.0, pts_offset_s=0.0, audio_sample_rate_index=3), msn=1)
    second = _analysed(
        SegmentSpec(duration=6.0, pts_offset_s=6.0, audio_sample_rate_index=4), msn=2
    )
    findings = segment_rules.check_segment_pair(
        segment_rules.RungHistory(variant="720p", last=first),
        second,
        layer=LAYER,
        thresholds=T,
        discontinuity_before=False,
    )
    change = next(f for f in findings if f.rule.id == "AUD-001")
    assert change.severity is Severity.CRITICAL
    assert "sample_rate 48000 -> 44100" in change.detail


def test_an_unchanged_aac_configuration_does_not_fire() -> None:
    first = _analysed(SegmentSpec(duration=6.0, pts_offset_s=0.0), msn=1)
    second = _analysed(SegmentSpec(duration=6.0, pts_offset_s=6.0), msn=2)
    findings = segment_rules.check_segment_pair(
        segment_rules.RungHistory(variant="720p", last=first),
        second,
        layer=LAYER,
        thresholds=T,
        discontinuity_before=False,
    )
    assert "AUD-001" not in ids(findings)


def test_av_skew_above_the_error_threshold_fires_and_a_normal_skew_does_not() -> None:
    drifted = _analysed(SegmentSpec(audio_pts_offset_ms=400.0))
    normal = _analysed(SegmentSpec(audio_pts_offset_ms=20.0))
    assert "AV-001" in ids(
        segment_rules.check_segment(drifted, variant="720p", layer=LAYER, thresholds=T)
    )
    assert "AV-001" not in ids(
        segment_rules.check_segment(normal, variant="720p", layer=LAYER, thresholds=T)
    )


def test_an_av_delta_above_one_second_is_critical() -> None:
    analysis = _analysed(SegmentSpec(audio_pts_offset_ms=1500.0))
    findings = segment_rules.check_segment(analysis, variant="720p", layer=LAYER, thresholds=T)
    critical = next(f for f in findings if f.rule.id == "AV-005")
    assert critical.severity is Severity.CRITICAL


def test_growing_av_skew_across_segments_fires() -> None:
    history = segment_rules.RungHistory(variant="720p")
    previous = _analysed(SegmentSpec(audio_pts_offset_ms=0.0), msn=0)
    history.last = previous
    for index, offset in enumerate([10.0, 30.0, 60.0, 100.0, 150.0], start=1):
        analysis = _analysed(
            SegmentSpec(duration=6.0, pts_offset_s=index * 6.0, audio_pts_offset_ms=offset),
            msn=index,
        )
        findings = segment_rules.check_segment_pair(
            history, analysis, layer=LAYER, thresholds=T, discontinuity_before=False
        )
    assert "AV-002" in ids(findings)


def test_muxed_segment_without_audio_fires() -> None:
    analysis = _analysed(SegmentSpec(with_audio=False))
    assert "AUD-003" in ids(
        segment_rules.check_segment(analysis, variant="720p", layer=LAYER, thresholds=T)
    )


def test_rungs_with_different_reference_frame_counts_fire_the_tizen_dpb_rule() -> None:
    mismatched = {
        "v1080": {"max_num_ref_frames": 4, "resolution": "1920x1080", "dpb_footprint": 8_294_400},
        "v360": {"max_num_ref_frames": 16, "resolution": "640x360", "dpb_footprint": 3_686_400},
    }
    aligned = {
        "v1080": {"max_num_ref_frames": 4, "resolution": "1920x1080"},
        "v360": {"max_num_ref_frames": 4, "resolution": "640x360"},
    }
    finding = segment_rules.check_dpb_across_rungs(mismatched, layer=LAYER)
    assert finding is not None
    assert finding.rule.id == "VID-006"
    assert "max_num_ref_frames=16" in finding.detail
    assert segment_rules.check_dpb_across_rungs(aligned, layer=LAYER) is None


def test_keyframes_misaligned_across_rungs_fire() -> None:
    aligned = {
        "a": segment_rules.RungHistory(variant="a", keyframe_msns={1, 2, 3}),
        "b": segment_rules.RungHistory(variant="b", keyframe_msns={1, 2, 3}),
    }
    misaligned = {
        "a": segment_rules.RungHistory(variant="a", keyframe_msns={1, 2, 3}),
        "b": segment_rules.RungHistory(variant="b", keyframe_msns={1, 3}),
    }
    assert segment_rules.check_keyframe_alignment(aligned, layer=LAYER) == []
    assert "VID-007" in ids(segment_rules.check_keyframe_alignment(misaligned, layer=LAYER))


def test_a_video_range_with_no_covering_audio_segment_fires() -> None:
    findings = segment_rules.check_demuxed_audio_coverage(
        video_ranges=[(1, 0.0, 6.0), (2, 6.0, 12.0)],
        audio_ranges=[(1, 0.0, 6.0)],
        variant="720p",
        layer=LAYER,
    )
    assert "AUD-006" in ids(findings)

    covered = segment_rules.check_demuxed_audio_coverage(
        video_ranges=[(1, 0.0, 6.0), (2, 6.0, 12.0)],
        audio_ranges=[(1, 0.0, 6.0), (2, 6.0, 12.0)],
        variant="720p",
        layer=LAYER,
    )
    assert "AUD-006" not in ids(covered)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def test_download_ratio_above_one_is_an_error_and_a_fast_download_is_silent() -> None:
    slow = transport_rules.check_download_ratio(
        download_ms=7000,
        declared_duration=6.0,
        url="s.ts",
        variant="720p",
        layer=LAYER,
        thresholds=T,
    )
    warn = transport_rules.check_download_ratio(
        download_ms=4000,
        declared_duration=6.0,
        url="s.ts",
        variant="720p",
        layer=LAYER,
        thresholds=T,
    )
    fast = transport_rules.check_download_ratio(
        download_ms=600,
        declared_duration=6.0,
        url="s.ts",
        variant="720p",
        layer=LAYER,
        thresholds=T,
    )
    assert slow is not None and slow.rule.id == "SEG-017"
    assert warn is not None and warn.rule.id == "CDN-006"
    assert fast is None


def test_throughput_below_the_declared_bandwidth_fires() -> None:
    low = transport_rules.check_throughput(
        measured_bps=500_000, declared_bandwidth=2_000_000, url="s.ts", variant="720p", layer=LAYER
    )
    fine = transport_rules.check_throughput(
        measured_bps=5_000_000,
        declared_bandwidth=2_000_000,
        url="s.ts",
        variant="720p",
        layer=LAYER,
    )
    assert low is not None and low.rule.id == "CDN-007"
    assert fine is None


def test_a_cdn_serving_an_older_msn_than_the_origin_fires() -> None:
    finding = transport_rules.check_cdn_behind_origin(
        cdn_msn=100, origin_msn=104, variant="720p", cdn_headers={"x-cache": "HIT"}
    )
    assert finding is not None and finding.rule.id == "CDN-003"
    assert (
        transport_rules.check_cdn_behind_origin(
            cdn_msn=104, origin_msn=104, variant="720p", cdn_headers={}
        )
        is None
    )


# ---------------------------------------------------------------------------
# Ads and subtitles
# ---------------------------------------------------------------------------


def test_an_unbalanced_cue_out_fires_and_a_closed_break_passes() -> None:
    open_break = parse_media(
        render_media_playlist(PlaylistSpec(segment_count=8, cue_out_at=2)), BASE
    )
    closed = parse_media(
        render_media_playlist(
            PlaylistSpec(
                segment_count=10,
                cue_out_at=2,
                cue_in_at=7,
                # Segments 2..7 inclusive carry the break: six segments of six seconds.
                cue_out_duration=36.0,
                discontinuity_at={2, 7},
            )
        ),
        BASE,
    )
    assert "ADS-010" in ids(
        ads_rules.check_cue_windows(open_break, variant="720p", layer=LAYER, thresholds=T)
    )
    closed_ids = ids(ads_rules.check_cue_windows(closed, variant="720p", layer=LAYER, thresholds=T))
    assert "ADS-010" not in closed_ids
    assert "ADS-900" in closed_ids


def test_a_break_missing_its_discontinuity_fires() -> None:
    playlist = parse_media(
        render_media_playlist(
            PlaylistSpec(segment_count=10, cue_out_at=2, cue_in_at=7, cue_out_duration=30.0)
        ),
        BASE,
    )
    assert "ADS-003" in ids(
        ads_rules.check_cue_windows(playlist, variant="720p", layer=LAYER, thresholds=T)
    )


def test_a_break_delivering_a_different_duration_fires() -> None:
    playlist = parse_media(
        render_media_playlist(
            PlaylistSpec(
                segment_count=12,
                cue_out_at=2,
                cue_in_at=9,
                cue_out_duration=15.0,
                discontinuity_at={2, 9},
            )
        ),
        BASE,
    )
    assert "ADS-005" in ids(
        ads_rules.check_cue_windows(playlist, variant="720p", layer=LAYER, thresholds=T)
    )


def test_a_creative_encoded_differently_from_the_content_fires() -> None:
    finding = ads_rules.check_creative_configuration(
        content_sps={"resolution": "1280x720", "profile": "High", "max_num_ref_frames": 4},
        ad_sps={"resolution": "640x360", "profile": "Main", "max_num_ref_frames": 2},
        content_audio={"sample_rate": 48000},
        ad_audio={"sample_rate": 44100},
        variant="720p",
        layer=LAYER,
        msn=500,
    )
    assert finding is not None and finding.rule.id == "ADS-002"
    assert finding.severity is Severity.CRITICAL

    matched = ads_rules.check_creative_configuration(
        content_sps={"resolution": "1280x720"},
        ad_sps={"resolution": "1280x720"},
        content_audio={"sample_rate": 48000},
        ad_audio={"sample_rate": 48000},
        variant="720p",
        layer=LAYER,
        msn=500,
    )
    assert matched is None


def test_webvtt_without_a_timestamp_map_fires_and_a_good_cue_file_passes() -> None:
    bad = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello\n"
    good = (
        "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000\n\n"
        "00:00:01.000 --> 00:00:03.000\nHello\n"
    )
    assert "SUB-002" in ids(
        subtitle_rules.check_webvtt(bad, variant="sub", uri="s.vtt", layer=LAYER)
    )
    assert ids(subtitle_rules.check_webvtt(good, variant="sub", uri="s.vtt", layer=LAYER)) == {
        "SUB-901"
    }


def test_a_cue_with_more_than_two_hour_digits_parses() -> None:
    """
    WebVTT allows two *or more* digits in the hours field.

    A packager that offsets cues from a channel epoch writes four, and the segment below is
    one the operator pulled from the CDN. Requiring exactly two reported every such segment
    as unparseable, which named the stream for text it had written correctly.
    """
    text = (
        "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:1246:04:14.730,MPEGTS:207000\n\n"
        "1247:44:25.511 --> 1247:44:27.113\nSo funny.\n\n"
        "1247:44:27.113 --> 1247:44:29.382\nYou had it in the bucket\n"
    )
    findings = subtitle_rules.check_webvtt(text, variant="sub_en", uri="s.vtt", layer=LAYER)
    assert ids(findings) == {"SUB-901"}
    assert "2 cue(s)" in findings[0].detail


def test_a_cue_whose_milliseconds_are_short_still_fires() -> None:
    """The hours field widened; the rest of the timestamp is still exact."""
    text = (
        "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
        "1247:44:27.11 --> 1247:44:29.382\nShort milliseconds\n"
    )
    assert "SUB-001" in ids(
        subtitle_rules.check_webvtt(text, variant="sub", uri="s.vtt", layer=LAYER)
    )


def test_a_webvtt_body_is_cut_on_a_line_boundary_before_the_cue_checks_see_it() -> None:
    """A half-written timing line is the analyzer's truncation, never the packager's text."""
    from app.media.segment import WEBVTT_HEAD_BYTES, analyse

    cues = "".join(
        f"00:{n // 60:02d}:{n % 60:02d}.000 --> 00:{(n + 1) // 60:02d}:{(n + 1) % 60:02d}.000\n"
        f"Line {n}\n\n"
        for n in range(400)
    )
    body = "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n" + cues
    assert len(body.encode()) > WEBVTT_HEAD_BYTES

    head = analyse(body.encode(), uri="s.vtt").raw["webvtt_head"]
    assert head.endswith("\n")
    assert ids(subtitle_rules.check_webvtt(head, variant="sub", uri="s.vtt", layer=LAYER)) == {
        "SUB-901"
    }


def test_control_characters_in_a_cue_fire() -> None:
    text = "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n00:00:01.000 --> 00:00:02.000\nHi\x01\n"
    assert "SUB-003" in ids(
        subtitle_rules.check_webvtt(text, variant="sub", uri="s.vtt", layer=LAYER)
    )


def test_captions_present_on_some_rungs_and_absent_on_others_fire() -> None:
    findings = subtitle_rules.check_caption_consistency(
        {"v720": True, "v360": False},
        {"v720": "cc1", "v360": "cc1"},
        layer=LAYER,
    )
    assert "SUB-006" in ids(findings)
