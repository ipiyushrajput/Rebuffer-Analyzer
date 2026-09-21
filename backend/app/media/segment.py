"""Container detection and a single per-segment analysis record.

Every collector produces a `SegmentAnalysis`; every segment, bitstream, audio and A/V rule
reads one. Keeping the shape identical for TS, fMP4 and packed audio is what lets the rule
modules stay container-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.media import adts, fmp4, ts


def detect_container(data: bytes, uri: str = "") -> str:
    """Identify the container from the bytes, falling back to the URI extension."""
    if not data:
        return "empty"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xF0) == 0xF0):
        return "aac"
    if data[:6] == b"WEBVTT" or data[:9] == b"\xef\xbb\xbfWEBVTT":
        return "webvtt"
    if (
        data[0] == ts.SYNC_BYTE
        and len(data) >= ts.PACKET_SIZE * 2
        and data[ts.PACKET_SIZE] == ts.SYNC_BYTE
    ):
        return "ts"
    if fmp4.looks_like_fmp4(data):
        return "fmp4"
    if data[:2] == b"\x0b\x77":
        return "ac3"
    lowered = uri.lower().split("?")[0]
    for suffix, name in (
        (".ts", "ts"),
        (".m4s", "fmp4"),
        (".mp4", "fmp4"),
        (".cmf", "fmp4"),
        (".aac", "aac"),
        (".vtt", "webvtt"),
        (".ac3", "ac3"),
        (".ec3", "ac3"),
    ):
        if lowered.endswith(suffix):
            return name
    return "unknown"


@dataclass(slots=True)
class SegmentAnalysis:
    """Measured facts about one segment. Nothing here is inferred."""

    uri: str
    container: str
    byte_size: int
    variant_id: str = ""
    msn: int = -1
    declared_duration: float | None = None
    actual_duration: float | None = None
    video_first_pts: int | None = None
    video_last_pts: int | None = None
    audio_first_pts: int | None = None
    audio_last_pts: int | None = None
    av_skew_ms: float | None = None
    has_video: bool = False
    has_audio: bool = False
    starts_with_keyframe: bool | None = None
    video_codec: str = ""
    audio_codec: str = ""
    sps: dict[str, Any] | None = None
    aac_config: dict[str, Any] | None = None
    ac3_config: dict[str, Any] | None = None
    pat_present: bool | None = None
    pmt_present: bool | None = None
    continuity_errors: int = 0
    has_sps: bool | None = None
    has_pps: bool | None = None
    has_vps: bool | None = None
    id3_timestamp: int | None = None
    scte35: list[dict[str, Any]] = field(default_factory=list)
    encrypted: bool = False
    # Why a protected payload was not decrypted, where it was not. Stated by INFO-001 so a
    # reader knows whether the key server refused, the scheme is one this analyzer does not
    # decrypt, or nothing was configured.
    drm_reason: str = ""
    parse_error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def measured_bitrate_bps(self) -> float | None:
        duration = self.actual_duration or self.declared_duration
        if not duration or duration <= 0:
            return None
        return self.byte_size * 8 / duration

    @property
    def duration_delta_s(self) -> float | None:
        if self.actual_duration is None or self.declared_duration is None:
            return None
        return self.actual_duration - self.declared_duration

    @property
    def config_key(self) -> tuple[Any, ...]:
        """Identity of the decoder configuration, compared between consecutive segments."""
        sps = self.sps or {}
        aac = self.aac_config or {}
        return (
            sps.get("codec"),
            sps.get("profile"),
            sps.get("resolution"),
            sps.get("max_num_ref_frames"),
            aac.get("aot"),
            aac.get("sample_rate"),
            aac.get("channel_config"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "container": self.container,
            "bytes": self.byte_size,
            "variant_id": self.variant_id,
            "msn": self.msn,
            "declared_duration": self.declared_duration,
            "actual_duration": self.actual_duration,
            "duration_delta_s": self.duration_delta_s,
            "measured_bitrate_bps": self.measured_bitrate_bps,
            "av_skew_ms": self.av_skew_ms,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
            "starts_with_keyframe": self.starts_with_keyframe,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "sps": self.sps,
            "aac_config": self.aac_config,
            "ac3_config": self.ac3_config,
            "pat_present": self.pat_present,
            "pmt_present": self.pmt_present,
            "continuity_errors": self.continuity_errors,
            "id3_timestamp": self.id3_timestamp,
            "encrypted": self.encrypted,
            "drm_reason": self.drm_reason,
            "parse_error": self.parse_error,
        }


def analyse(
    data: bytes,
    *,
    uri: str = "",
    declared_duration: float | None = None,
    variant_id: str = "",
    msn: int = -1,
    init_segment: fmp4.Fmp4Analysis | None = None,
    encrypted: bool = False,
) -> SegmentAnalysis:
    """Parse one segment into the shared record."""
    container = detect_container(data, uri)
    analysis = SegmentAnalysis(
        uri=uri,
        container=container,
        byte_size=len(data),
        variant_id=variant_id,
        msn=msn,
        declared_duration=declared_duration,
        encrypted=encrypted,
    )

    if encrypted:
        # Bitstream checks do not run on an encrypted payload; the caller records that as a
        # definite INFO finding rather than reporting a missing keyframe that is simply
        # unreadable.
        analysis.parse_error = "Payload is encrypted; bitstream checks did not run"
        return analysis

    if container == "ts":
        _analyse_ts(data, analysis)
    elif container == "fmp4":
        _analyse_fmp4(data, analysis, init_segment)
    elif container == "aac":
        _analyse_packed_audio(data, analysis)
    elif container == "ac3":
        config = adts.parse_ac3(data)
        analysis.has_audio = config is not None
        analysis.ac3_config = config.as_dict() if config else None
        analysis.audio_codec = config.codec if config else ""
        analysis.id3_timestamp = adts.find_id3_timestamp(data)
    elif container == "webvtt":
        analysis.raw["webvtt_head"] = _webvtt_head(data)
    elif container == "empty":
        analysis.parse_error = "Segment body is empty"
    else:
        analysis.parse_error = f"Container is not recognised from the first bytes or the URI: {uri}"

    return analysis


WEBVTT_HEAD_BYTES = 8192


def _webvtt_head(data: bytes) -> str:
    """The head of a WebVTT segment, cut only on a line boundary.

    The cue checks judge whole lines. Cutting mid-line handed them a half-written timing
    line, which reported the stream for a cue the packager had written correctly, so the
    trailing partial line is dropped whenever the body is longer than the cap.
    """
    if len(data) <= WEBVTT_HEAD_BYTES:
        return data.decode("utf-8", errors="replace")
    text = data[:WEBVTT_HEAD_BYTES].decode("utf-8", errors="replace")
    cut = text.rfind("\n")
    return text[: cut + 1] if cut != -1 else ""


def _analyse_ts(data: bytes, analysis: SegmentAnalysis) -> None:
    from app.hls import scte35 as scte35_module

    parsed = ts.parse(data)
    ts.enrich_codec_details(parsed)

    analysis.pat_present = parsed.pat_present
    analysis.pmt_present = parsed.pmt_present
    analysis.continuity_errors = parsed.continuity_errors
    analysis.has_video = parsed.has_video
    analysis.has_audio = parsed.has_audio
    analysis.av_skew_ms = parsed.av_skew_ms
    analysis.raw["ts"] = parsed.as_dict()
    if not parsed.sync_ok:
        found = parsed.first_byte or 0
        analysis.parse_error = (
            f"Transport stream sync byte 0x47 is absent at offset 0 (found 0x{found:02x})"
        )

    video = parsed.video_track
    if video:
        analysis.video_codec = video.codec_name
        analysis.video_first_pts = video.first_pts
        analysis.video_last_pts = video.last_pts
        details = video.codec_details
        analysis.sps = details.get("sps")
        analysis.has_sps = details.get("has_sps")
        analysis.has_pps = details.get("has_pps")
        analysis.has_vps = details.get("has_vps")
        analysis.starts_with_keyframe = details.get("starts_with_idr")
        if video.duration_s is not None:
            analysis.actual_duration = video.duration_s

    audio_tracks = parsed.audio_tracks
    if audio_tracks:
        audio = audio_tracks[0]
        analysis.audio_codec = audio.codec_name
        analysis.audio_first_pts = audio.first_pts
        analysis.audio_last_pts = audio.last_pts
        analysis.aac_config = audio.codec_details.get("aac")
        analysis.ac3_config = audio.codec_details.get("ac3")
        if analysis.actual_duration is None and audio.duration_s is not None:
            analysis.actual_duration = audio.duration_s

    for section in parsed.scte35_sections:
        info = scte35_module.decode_section(section)
        if info:
            analysis.scte35.append(info.as_dict())


def _analyse_fmp4(
    data: bytes, analysis: SegmentAnalysis, init_segment: fmp4.Fmp4Analysis | None
) -> None:
    parsed = fmp4.parse_segment(data)
    analysis.raw["fmp4"] = parsed.as_dict()
    tracks = dict(init_segment.tracks) if init_segment else {}
    tracks.update(parsed.tracks)

    if not parsed.has_moof and not parsed.is_init:
        analysis.parse_error = "fMP4 segment carries no moof box"

    for track_id, base in parsed.base_media_decode_time.items():
        track = tracks.get(track_id)
        timescale = track.timescale if track else 0
        if not timescale:
            continue
        duration = parsed.sample_duration_sum.get(track_id, 0) / timescale
        start_pts = int(base / timescale * ts.PTS_HZ)
        end_pts = int((base + parsed.sample_duration_sum.get(track_id, 0)) / timescale * ts.PTS_HZ)
        handler = (track.handler if track else "").lower()
        codec = track.codec if track else ""
        if handler == "vide" or codec.startswith(("avc", "hvc", "hev", "dvh")):
            analysis.has_video = True
            analysis.video_codec = codec
            analysis.video_first_pts = start_pts
            analysis.video_last_pts = end_pts
            analysis.sps = track.sps if track else None
            analysis.actual_duration = duration
            # A CMAF segment is required to start at a random-access point; the styp brand
            # or the presence of an init-carried parameter set is what proves it.
            analysis.starts_with_keyframe = bool(track and track.sps) or parsed.styp is not None
        elif handler == "soun" or codec in ("mp4a", "ac-3", "ec-3", "ac-4"):
            analysis.has_audio = True
            analysis.audio_codec = codec
            analysis.audio_first_pts = start_pts
            analysis.audio_last_pts = end_pts
            if track and track.sample_rate:
                analysis.aac_config = {
                    "sample_rate": track.sample_rate,
                    "channels": track.channels,
                    "aot": None,
                    "channel_config": track.channels,
                }
            if analysis.actual_duration is None:
                analysis.actual_duration = duration

    if analysis.video_first_pts is not None and analysis.audio_first_pts is not None:
        analysis.av_skew_ms = (
            ts.pts_diff(analysis.audio_first_pts, analysis.video_first_pts) / ts.PTS_HZ * 1000.0
        )


def _analyse_packed_audio(data: bytes, analysis: SegmentAnalysis) -> None:
    analysis.id3_timestamp = adts.find_id3_timestamp(data)
    config = adts.parse_adts(data)
    analysis.has_audio = config is not None
    if config:
        analysis.aac_config = config.as_dict()
        analysis.audio_codec = "aac"
        analysis.actual_duration = config.duration_s
        if analysis.id3_timestamp is not None:
            analysis.audio_first_pts = analysis.id3_timestamp
            analysis.audio_last_pts = analysis.id3_timestamp + int(config.duration_s * ts.PTS_HZ)
    else:
        analysis.parse_error = "No ADTS sync word found in a segment declared as packed audio"
