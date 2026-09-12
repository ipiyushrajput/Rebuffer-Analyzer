"""Playlist parsing: raw lines, absolute sequence numbers, and URI resolution."""

from __future__ import annotations

from app.hls import scte35
from app.hls.playlist import is_master, parse, parse_master, parse_media
from tests.fixtures.synth import (
    PlaylistSpec,
    VariantSpec,
    render_master_playlist,
    render_media_playlist,
)

BASE = "https://cdn.example/live/ch1/master.m3u8?hdnts=abc"


def test_master_and_media_are_distinguished() -> None:
    master = render_master_playlist([VariantSpec(name="a", bandwidth=1000)])
    media = render_media_playlist(PlaylistSpec())
    assert is_master(master) is True
    assert is_master(media) is False
    assert type(parse(master, BASE)).__name__ == "MasterPlaylist"
    assert type(parse(media, BASE)).__name__ == "MediaPlaylist"


def test_master_variants_carry_attributes_and_resolved_uris() -> None:
    text = render_master_playlist(
        [
            VariantSpec(name="low", bandwidth=600_000, resolution="640x360", uri="low/index.m3u8"),
            VariantSpec(name="high", bandwidth=3_000_000, resolution="1920x1080"),
        ],
        audio_renditions=[("aac", "English", "audio/en.m3u8")],
    )
    master = parse_master(text, BASE)

    assert len(master.variants) == 2
    assert master.variants[0].bandwidth == 600_000
    assert master.variants[0].height == 360
    assert master.variants[0].frame_rate == 30.0
    assert master.variants[0].resolved_uri == "https://cdn.example/live/ch1/low/index.m3u8"
    assert master.independent_segments is True
    assert master.first_line == "#EXTM3U"

    audio = master.audio_renditions()
    assert len(audio) == 1
    assert audio[0].language == "en"
    assert audio[0].resolved_uri == "https://cdn.example/live/ch1/audio/en.m3u8"


def test_variant_line_numbers_are_kept_for_evidence() -> None:
    master = parse_master(render_master_playlist([VariantSpec(name="a", bandwidth=1)]), BASE)
    line = master.variants[0].line
    assert line.number > 0
    assert line.text.startswith("#EXT-X-STREAM-INF:")


def test_missing_attributes_are_reported_as_absent_not_defaulted() -> None:
    text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nv.m3u8\n"
    master = parse_master(text, BASE)
    variant = master.variants[0]
    assert variant.resolution is None
    assert variant.frame_rate is None
    assert variant.codecs is None


def test_media_playlist_assigns_absolute_msn_per_segment() -> None:
    spec = PlaylistSpec(media_sequence=48210, segment_count=4)
    media = parse_media(render_media_playlist(spec), BASE)
    assert [s.msn for s in media.segments] == [48210, 48211, 48212, 48213]
    assert media.last_msn == 48213
    assert media.target_duration == 6


def test_discontinuity_sequence_is_absolute_per_segment() -> None:
    spec = PlaylistSpec(
        media_sequence=100, segment_count=5, discontinuity_sequence=7, discontinuity_at={2, 4}
    )
    media = parse_media(render_media_playlist(spec), BASE)
    # The counter starts at the playlist value and steps at every in-window tag, so a
    # boundary stays correct after earlier tags scroll out of the window.
    assert [s.discontinuity_sequence for s in media.segments] == [7, 7, 8, 8, 9]
    assert media.segments[2].discontinuity_before is True
    assert media.segments[1].discontinuity_before is False


def test_segment_uris_resolve_against_the_playlist_final_url() -> None:
    media = parse_media(
        render_media_playlist(PlaylistSpec(segment_count=1, segment_prefix="s")),
        "https://edge.example/a/b/720p.m3u8?token=1",
    )
    assert media.segments[0].resolved_uri == "https://edge.example/a/b/s0.ts"


def test_live_and_vod_are_distinguished_by_endlist() -> None:
    live = parse_media(render_media_playlist(PlaylistSpec()), BASE)
    vod = parse_media(render_media_playlist(PlaylistSpec(endlist=True)), BASE)
    assert live.is_live is True
    assert vod.is_live is False
    assert vod.endlist is True


def test_program_date_time_and_gap_tags_are_attached_to_segments() -> None:
    spec = PlaylistSpec(segment_count=3, gap_at={1}, program_date_time="2026-09-11T10:00:00.000Z")
    media = parse_media(render_media_playlist(spec), BASE)
    assert media.segments[0].program_date_time == "2026-09-11T10:00:00.000Z"
    assert media.segments[1].gap is True
    assert media.segments[2].gap is False


def test_unparseable_extinf_becomes_a_negative_duration_not_an_exception() -> None:
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:abc,\ns0.ts\n"
    media = parse_media(text, BASE)
    assert media.segments[0].duration == -1.0


def test_cue_out_and_cue_in_pair_into_one_window() -> None:
    spec = PlaylistSpec(
        segment_count=8, cue_out_at=2, cue_in_at=6, cue_out_duration=24.0, segment_duration=6.0
    )
    media = parse_media(render_media_playlist(spec), BASE)
    windows = scte35.extract_cue_windows(media)
    assert len(windows) == 1
    assert windows[0].closed is True
    assert windows[0].declared_duration_s == 24.0
    assert windows[0].segment_count == 5
    assert windows[0].delivered_duration_s == 30.0
    assert windows[0].duration_delta_s == 6.0


def test_an_unclosed_cue_out_is_reported_as_open() -> None:
    spec = PlaylistSpec(segment_count=6, cue_out_at=1)
    media = parse_media(render_media_playlist(spec), BASE)
    windows = scte35.extract_cue_windows(media)
    assert windows[0].closed is False
    assert windows[0].end_msn is None


def test_ext_x_map_is_recorded_and_resolved() -> None:
    spec = PlaylistSpec(segment_count=2, map_uri="init.mp4")
    media = parse_media(render_media_playlist(spec), "https://e/x/y/v.m3u8")
    assert media.map_uri == "https://e/x/y/init.mp4"
    assert media.segments[0].map_uri == "https://e/x/y/init.mp4"


def test_encryption_is_detected_from_ext_x_key() -> None:
    text = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="https://k/key.bin",IV=0x0\n'
        "#EXTINF:6.0,\ns0.ts\n"
    )
    media = parse_media(text, BASE)
    assert media.is_encrypted is True
    assert media.segments[0].key is not None
    assert media.segments[0].key["METHOD"] == "AES-128"


def test_a_media_playlist_that_became_a_master_is_flagged() -> None:
    text = render_master_playlist([VariantSpec(name="a", bandwidth=1)])
    media = parse_media(text, BASE)
    assert media.looks_like_master is True
