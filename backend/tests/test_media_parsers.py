"""Bitstream and container parsers, driven by synthetic segments with known values."""

from __future__ import annotations

from app.media import adts, fmp4, segment, ts
from app.media.bitreader import BitReader, unescape_rbsp
from app.media.h264_sps import parse_sps
from tests.fixtures.synth import SegmentSpec, adts_frames, build_sps, build_ts_segment


def test_rbsp_unescaping_removes_emulation_prevention_bytes() -> None:
    assert unescape_rbsp(b"\x00\x00\x03\x01") == b"\x00\x00\x01"
    assert unescape_rbsp(b"\x00\x00\x03\x00\x00\x03\x02") == b"\x00\x00\x00\x00\x02"
    assert unescape_rbsp(b"\x01\x02\x03") == b"\x01\x02\x03"


def test_bitreader_exp_golomb_round_trip() -> None:
    reader = BitReader(b"\xa0", unescape=False)  # 1 010 0000 -> ue=0, se=-1... read explicitly
    assert reader.ue() == 0
    assert reader.u(1) == 0


def test_sps_decode_reports_resolution_and_ref_frames() -> None:
    raw = build_sps(width=1280, height=720, max_num_ref_frames=9, level_idc=31)
    sps = parse_sps(raw)
    assert sps is not None
    assert sps.width == 1280
    assert sps.height == 720
    assert sps.max_num_ref_frames == 9
    assert sps.profile_name == "High"
    assert sps.level == "3.1"
    assert sps.chroma_format == "4:2:0"
    assert sps.bit_depth_luma == 8
    assert sps.scan_type == "progressive"
    assert sps.frame_rate == 30.0


def test_sps_decode_distinguishes_ladder_dpb_footprints() -> None:
    high = parse_sps(build_sps(width=1920, height=1080, max_num_ref_frames=4))
    low = parse_sps(build_sps(width=640, height=360, max_num_ref_frames=16))
    assert high is not None and low is not None
    assert high.max_num_ref_frames != low.max_num_ref_frames
    assert high.dpb_footprint != low.dpb_footprint


def test_ts_parser_finds_pat_pmt_tracks_and_pts() -> None:
    data = build_ts_segment(SegmentSpec(duration=6.0, pts_offset_s=12.0))
    parsed = ts.parse(data)
    ts.enrich_codec_details(parsed)

    assert parsed.sync_ok is True
    assert parsed.pat_present is True
    assert parsed.pmt_present is True
    assert parsed.has_video is True
    assert parsed.has_audio is True
    assert parsed.continuity_errors == 0

    video = parsed.video_track
    assert video is not None
    assert video.first_pts == int(12.0 * ts.PTS_HZ)
    assert abs((video.duration_s or 0) - 6.0) < 0.01
    assert video.codec_details["has_sps"] is True
    assert video.codec_details["sps"]["resolution"] == "1280x720"


def test_ts_parser_reports_broken_sync_byte() -> None:
    data = build_ts_segment(SegmentSpec(broken_sync=True))
    parsed = ts.parse(data)
    assert parsed.sync_ok is False


def test_ts_parser_reports_missing_pat() -> None:
    parsed = ts.parse(build_ts_segment(SegmentSpec(missing_pat=True)))
    assert parsed.pat_present is False


def test_ts_parser_counts_continuity_errors() -> None:
    parsed = ts.parse(build_ts_segment(SegmentSpec(continuity_error=True)))
    assert parsed.continuity_errors >= 1


def test_av_skew_is_audio_minus_video_first_pts() -> None:
    parsed = ts.parse(build_ts_segment(SegmentSpec(audio_pts_offset_ms=250.0)))
    skew = parsed.av_skew_ms
    assert skew is not None
    assert abs(skew - 250.0) < 1.0


def test_pts_rollover_is_unwrapped_not_treated_as_backwards() -> None:
    near_wrap = ts.PTS_MODULUS - 1000
    wrapped = 500
    assert ts.pts_diff(wrapped, near_wrap) == 1500
    assert ts.unwrap_pts(wrapped, near_wrap) == near_wrap + 1500


def test_adts_config_is_read_from_raw_aac() -> None:
    config = adts.parse_adts(adts_frames(sample_rate_index=3, channel_config=2, frames=10))
    assert config is not None
    assert config.sample_rate == 48000
    assert config.channels == 2
    assert config.aot_name == "AAC-LC"
    assert config.frame_count == 10


def test_adts_config_change_is_visible_between_segments() -> None:
    first = adts.parse_adts(adts_frames(sample_rate_index=3))
    second = adts.parse_adts(adts_frames(sample_rate_index=4))
    assert first is not None and second is not None
    assert first.config_key() != second.config_key()


def test_packed_audio_without_id3_timestamp_is_detected() -> None:
    assert adts.has_id3_timestamp(adts_frames()) is False


def test_container_detection_covers_every_shape() -> None:
    assert segment.detect_container(build_ts_segment(SegmentSpec())) == "ts"
    assert segment.detect_container(adts_frames()) == "aac"
    assert segment.detect_container(b"WEBVTT\n\n") == "webvtt"
    assert segment.detect_container(b"") == "empty"
    assert segment.detect_container(b"\x00" * 40, "x.m4s") == "fmp4"


def test_segment_analysis_exposes_measured_values() -> None:
    data = build_ts_segment(SegmentSpec(duration=6.0, audio_pts_offset_ms=20.0))
    analysis = segment.analyse(data, uri="seg0.ts", declared_duration=6.0, msn=7)
    assert analysis.container == "ts"
    assert analysis.has_video and analysis.has_audio
    assert analysis.starts_with_keyframe is True
    assert analysis.sps is not None
    assert analysis.measured_bitrate_bps is not None
    assert abs((analysis.av_skew_ms or 0) - 20.0) < 1.0
    assert abs(analysis.duration_delta_s or 0) < 0.05


def test_segment_analysis_flags_non_keyframe_start() -> None:
    data = build_ts_segment(SegmentSpec(start_with_idr=False, include_sps=False))
    analysis = segment.analyse(data, uri="seg1.ts", declared_duration=6.0)
    assert analysis.starts_with_keyframe is False


def test_encrypted_segment_is_reported_as_not_analysed() -> None:
    analysis = segment.analyse(b"\x00" * 100, uri="s.ts", encrypted=True)
    assert analysis.parse_error == "Payload is encrypted; bitstream checks did not run"


def test_fmp4_box_walker_reads_nested_boxes() -> None:
    inner = b"\x00\x00\x00\x10mdhd" + b"\x00" * 8
    moov = (len(inner) + 8).to_bytes(4, "big") + b"mdia" + inner
    data = (len(moov) + 8).to_bytes(4, "big") + b"trak" + moov
    boxes = fmp4.find_boxes(data, (b"trak", b"mdia", b"mdhd"))
    assert len(boxes) == 1
    assert boxes[0].name == "mdhd"
