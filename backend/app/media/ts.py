"""MPEG-2 Transport Stream parsing.

Answers, per segment: is the sync byte where it belongs, are PAT and PMT present, did any
PID lose a packet (continuity counter), what are the first and last PTS of each elementary
stream, and is there an in-band SCTE-35 stream.

PTS is a 33-bit counter that wraps every ~26.5 hours. Every comparison in this module
unwraps it first, so a rollover is never mistaken for a backwards jump.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PACKET_SIZE = 188
SYNC_BYTE = 0x47
PTS_MODULUS = 1 << 33
PTS_HZ = 90000.0
# Half the counter: a difference larger than this is a wrap, not a real jump.
PTS_WRAP_GUARD = PTS_MODULUS // 2

STREAM_TYPE_NAMES = {
    0x01: "mpeg1video",
    0x02: "mpeg2video",
    0x03: "mp2",
    0x04: "mp3",
    0x0F: "aac",
    0x11: "aac_latm",
    0x15: "id3",
    0x1B: "h264",
    0x24: "hevc",
    0x81: "ac-3",
    0x86: "scte35",
    0x87: "ec-3",
}

VIDEO_STREAM_TYPES = frozenset({0x01, 0x02, 0x1B, 0x24})
AUDIO_STREAM_TYPES = frozenset({0x03, 0x04, 0x0F, 0x11, 0x81, 0x87})
SCTE35_STREAM_TYPE = 0x86


def unwrap_pts(current: int, previous: int | None) -> int:
    """Unwrap a 33-bit PTS against the previous value so arithmetic stays monotonic."""
    if previous is None:
        return current
    delta = current - (previous % PTS_MODULUS)
    if delta < -PTS_WRAP_GUARD:
        delta += PTS_MODULUS
    elif delta > PTS_WRAP_GUARD:
        delta -= PTS_MODULUS
    return previous + delta


def pts_diff(a: int, b: int) -> int:
    """Signed difference a - b, corrected for a 33-bit wrap between the two."""
    delta = (a % PTS_MODULUS) - (b % PTS_MODULUS)
    if delta < -PTS_WRAP_GUARD:
        delta += PTS_MODULUS
    elif delta > PTS_WRAP_GUARD:
        delta -= PTS_MODULUS
    return delta


@dataclass(slots=True)
class TrackInfo:
    pid: int
    stream_type: int
    first_pts: int | None = None
    last_pts: int | None = None
    first_dts: int | None = None
    pts_count: int = 0
    continuity_errors: int = 0
    bytes_seen: int = 0
    nal_types: list[int] = field(default_factory=list)
    codec_details: dict[str, Any] = field(default_factory=dict)
    payload_head: bytes = b""

    @property
    def kind(self) -> str:
        if self.stream_type in VIDEO_STREAM_TYPES:
            return "video"
        if self.stream_type in AUDIO_STREAM_TYPES:
            return "audio"
        if self.stream_type == SCTE35_STREAM_TYPE:
            return "scte35"
        return "other"

    @property
    def codec_name(self) -> str:
        return STREAM_TYPE_NAMES.get(self.stream_type, f"0x{self.stream_type:02x}")

    @property
    def duration_s(self) -> float | None:
        if self.first_pts is None or self.last_pts is None:
            return None
        return pts_diff(self.last_pts, self.first_pts) / PTS_HZ

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "stream_type": self.stream_type,
            "codec": self.codec_name,
            "kind": self.kind,
            "first_pts": self.first_pts,
            "last_pts": self.last_pts,
            "pts_count": self.pts_count,
            "continuity_errors": self.continuity_errors,
            "duration_s": self.duration_s,
            "codec_details": self.codec_details,
        }


@dataclass(slots=True)
class TsAnalysis:
    packet_count: int = 0
    sync_ok: bool = True
    first_byte: int | None = None
    pat_present: bool = False
    pmt_present: bool = False
    pmt_pid: int | None = None
    program_number: int | None = None
    pcr_pid: int | None = None
    tracks: dict[int, TrackInfo] = field(default_factory=dict)
    continuity_errors: int = 0
    scte35_sections: list[bytes] = field(default_factory=list)
    trailing_bytes: int = 0
    error: str | None = None

    @property
    def video_track(self) -> TrackInfo | None:
        return next((t for t in self.tracks.values() if t.kind == "video"), None)

    @property
    def audio_tracks(self) -> list[TrackInfo]:
        return [t for t in self.tracks.values() if t.kind == "audio"]

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_tracks)

    @property
    def has_video(self) -> bool:
        return self.video_track is not None

    @property
    def av_skew_ms(self) -> float | None:
        """audio_first_pts - video_first_pts, in milliseconds."""
        video = self.video_track
        audio = self.audio_tracks[0] if self.audio_tracks else None
        if not video or not audio or video.first_pts is None or audio.first_pts is None:
            return None
        return pts_diff(audio.first_pts, video.first_pts) / PTS_HZ * 1000.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "container": "ts",
            "packets": self.packet_count,
            "sync_ok": self.sync_ok,
            "pat_present": self.pat_present,
            "pmt_present": self.pmt_present,
            "pmt_pid": self.pmt_pid,
            "pcr_pid": self.pcr_pid,
            "continuity_errors": self.continuity_errors,
            "tracks": [t.as_dict() for t in self.tracks.values()],
            "av_skew_ms": self.av_skew_ms,
            "scte35_section_count": len(self.scte35_sections),
            "error": self.error,
        }


def _read_pts(data: bytes, offset: int) -> int:
    return (
        ((data[offset] >> 1) & 0x07) << 30
        | data[offset + 1] << 22
        | ((data[offset + 2] >> 1) & 0x7F) << 15
        | data[offset + 3] << 7
        | ((data[offset + 4] >> 1) & 0x7F)
    )


def parse(data: bytes, *, collect_nal: bool = True, head_bytes: int = 65536) -> TsAnalysis:
    """Parse a TS segment. ``head_bytes`` caps how much elementary payload is retained."""
    analysis = TsAnalysis()
    if not data:
        analysis.error = "Segment body is empty"
        analysis.sync_ok = False
        return analysis

    analysis.first_byte = data[0]
    if data[0] != SYNC_BYTE:
        analysis.sync_ok = False

    analysis.trailing_bytes = len(data) % PACKET_SIZE
    pmt_pids: set[int] = set()
    continuity: dict[int, int] = {}
    pes_pending: dict[int, bytearray] = {}

    offset = 0
    total = len(data)
    while offset + PACKET_SIZE <= total:
        packet = data[offset : offset + PACKET_SIZE]
        offset += PACKET_SIZE
        if packet[0] != SYNC_BYTE:
            analysis.sync_ok = False
            # Re-acquire sync at the next sync byte rather than abandoning the segment.
            next_sync = data.find(bytes([SYNC_BYTE]), offset - PACKET_SIZE + 1)
            if next_sync < 0:
                break
            offset = next_sync
            continue

        analysis.packet_count += 1
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        payload_unit_start = bool(packet[1] & 0x40)
        adaptation_control = (packet[3] >> 4) & 0x03
        counter = packet[3] & 0x0F

        has_payload = adaptation_control in (1, 3)
        if has_payload and pid != 0x1FFF:
            previous = continuity.get(pid)
            if previous is not None and counter != (previous + 1) % 16:
                analysis.continuity_errors += 1
                track = analysis.tracks.get(pid)
                if track:
                    track.continuity_errors += 1
            continuity[pid] = counter

        payload_offset = 4
        if adaptation_control in (2, 3):
            adaptation_length = packet[4]
            if adaptation_control == 2:
                continue
            payload_offset = 5 + adaptation_length
            if payload_offset >= PACKET_SIZE:
                continue
        payload = packet[payload_offset:]
        if not payload:
            continue

        if pid == 0:
            analysis.pat_present = True
            pmt_pids |= _parse_pat(payload, payload_unit_start, analysis)
            continue

        if pid in pmt_pids:
            analysis.pmt_present = True
            analysis.pmt_pid = pid
            _parse_pmt(payload, payload_unit_start, analysis)
            continue

        track = analysis.tracks.get(pid)
        if track is None:
            continue

        if track.stream_type == SCTE35_STREAM_TYPE:
            if payload_unit_start and len(payload) > 1:
                analysis.scte35_sections.append(bytes(payload[payload[0] + 1 :]))
            continue

        track.bytes_seen += len(payload)
        if payload_unit_start and len(payload) >= 9 and payload[:3] == b"\x00\x00\x01":
            flags = payload[7]
            header_length = payload[8]
            pts: int | None = None
            if flags & 0x80 and len(payload) >= 14:
                pts = _read_pts(payload, 9)
            if flags & 0x40 and len(payload) >= 19:
                track.first_dts = (
                    track.first_dts if track.first_dts is not None else _read_pts(payload, 14)
                )
            if pts is not None:
                if track.first_pts is None:
                    track.first_pts = pts
                    track.last_pts = pts
                else:
                    track.last_pts = unwrap_pts(pts, track.last_pts)
                track.pts_count += 1
            es_offset = 9 + header_length
            if collect_nal and len(track.payload_head) < head_bytes:
                track.payload_head += bytes(payload[es_offset:])
            pes_pending[pid] = bytearray(payload[es_offset:])
        elif collect_nal and len(track.payload_head) < head_bytes:
            track.payload_head += bytes(payload)

    del pes_pending
    return analysis


def _parse_pat(payload: bytes, unit_start: bool, analysis: TsAnalysis) -> set[int]:
    pmt_pids: set[int] = set()
    if unit_start:
        pointer = payload[0]
        section = payload[1 + pointer :]
    else:
        section = payload
    if len(section) < 12 or section[0] != 0x00:
        return pmt_pids
    section_length = ((section[1] & 0x0F) << 8) | section[2]
    body_end = min(3 + section_length - 4, len(section))
    index = 8
    while index + 4 <= body_end:
        program_number = (section[index] << 8) | section[index + 1]
        pid = ((section[index + 2] & 0x1F) << 8) | section[index + 3]
        if program_number != 0:
            pmt_pids.add(pid)
            if analysis.program_number is None:
                analysis.program_number = program_number
        index += 4
    return pmt_pids


def _parse_pmt(payload: bytes, unit_start: bool, analysis: TsAnalysis) -> None:
    if unit_start:
        pointer = payload[0]
        section = payload[1 + pointer :]
    else:
        section = payload
    if len(section) < 12 or section[0] != 0x02:
        return
    section_length = ((section[1] & 0x0F) << 8) | section[2]
    analysis.pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    index = 12 + program_info_length
    body_end = min(3 + section_length - 4, len(section))
    while index + 5 <= body_end:
        stream_type = section[index]
        pid = ((section[index + 1] & 0x1F) << 8) | section[index + 2]
        es_info_length = ((section[index + 3] & 0x0F) << 8) | section[index + 4]
        if pid not in analysis.tracks:
            analysis.tracks[pid] = TrackInfo(pid=pid, stream_type=stream_type)
        index += 5 + es_info_length


def enrich_codec_details(analysis: TsAnalysis) -> None:
    """Decode parameter sets and audio configs from the retained elementary payload."""
    from app.media import adts, h264_sps, hevc_sps

    for track in analysis.tracks.values():
        head = track.payload_head
        if not head:
            continue
        if track.stream_type == 0x1B:
            units = h264_sps.iter_nal_units(head)
            track.nal_types = [t for t, _ in units]
            sps = next((h264_sps.parse_sps(p) for t, p in units if t == 7), None)
            track.codec_details = {
                "sps": sps.as_dict() if sps else None,
                "has_sps": any(t == 7 for t, _ in units),
                "has_pps": any(t == 8 for t, _ in units),
                "has_idr": any(t == 5 for t, _ in units),
                "starts_with_idr": bool(units) and units[0][0] in (5, 7, 8, 9),
            }
        elif track.stream_type == 0x24:
            units = hevc_sps.iter_nal_units(head)
            track.nal_types = [t for t, _ in units]
            sps = next((hevc_sps.parse_sps(p) for t, p in units if t == hevc_sps.NAL_SPS), None)
            track.codec_details = {
                "sps": sps.as_dict() if sps else None,
                "has_vps": any(t == hevc_sps.NAL_VPS for t, _ in units),
                "has_sps": any(t == hevc_sps.NAL_SPS for t, _ in units),
                "has_pps": any(t == hevc_sps.NAL_PPS for t, _ in units),
                "has_irap": hevc_sps.has_irap(track.nal_types),
                "starts_with_idr": hevc_sps.has_irap(track.nal_types[:4]),
            }
        elif track.stream_type in (0x0F, 0x11):
            config = adts.parse_adts(head)
            track.codec_details = {"aac": config.as_dict() if config else None}
        elif track.stream_type in (0x81, 0x87):
            config = adts.parse_ac3(head)
            track.codec_details = {"ac3": config.as_dict() if config else None}
