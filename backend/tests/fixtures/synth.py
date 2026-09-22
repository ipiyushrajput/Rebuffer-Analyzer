"""Synthetic HLS stream builder.

`generate.py` uses ffmpeg when the binary is installed. This module builds the same shapes
in pure Python so the rule tests run on any machine and so a fault can be injected exactly
where a rule expects it — a PTS gap of a chosen size, an AAC sample-rate change at a chosen
segment, a segment that starts on a non-IDR slice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.media.ts import PACKET_SIZE, PTS_HZ, SYNC_BYTE

PAT_PID = 0x0000
PMT_PID = 0x0100
VIDEO_PID = 0x0101
AUDIO_PID = 0x0102
SCTE35_PID = 0x0103

STREAM_TYPE_H264 = 0x1B
STREAM_TYPE_AAC = 0x0F
STREAM_TYPE_SCTE35 = 0x86


def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if crc & 0x80000000
                else (crc << 1) & 0xFFFFFFFF
            )
    return crc


def _section(table_id: int, body: bytes) -> bytes:
    length = len(body) + 4
    header = bytes([table_id, 0xB0 | ((length >> 8) & 0x0F), length & 0xFF])
    section = header + body
    return section + _crc32_mpeg(section).to_bytes(4, "big")


def _ts_packet(
    pid: int, payload: bytes, *, unit_start: bool, counter: int, pcr: int | None = None
) -> bytes:
    header = bytearray(4)
    header[0] = SYNC_BYTE
    header[1] = (0x40 if unit_start else 0) | ((pid >> 8) & 0x1F)
    header[2] = pid & 0xFF
    adaptation = b""
    if pcr is not None:
        base = pcr & ((1 << 33) - 1)
        adaptation_body = bytes(
            [
                0x10,  # PCR flag
                (base >> 25) & 0xFF,
                (base >> 17) & 0xFF,
                (base >> 9) & 0xFF,
                (base >> 1) & 0xFF,
                ((base & 1) << 7) | 0x7E,
                0x00,
            ]
        )
        adaptation = bytes([len(adaptation_body)]) + adaptation_body
        header[3] = 0x30 | (counter & 0x0F)
    else:
        header[3] = 0x10 | (counter & 0x0F)

    space = PACKET_SIZE - 4 - len(adaptation)
    if len(payload) < space:
        stuffing = space - len(payload)
        if adaptation:
            adaptation = bytes([adaptation[0] + stuffing]) + adaptation[1:] + b"\xff" * stuffing
        else:
            header[3] = 0x30 | (counter & 0x0F)
            if stuffing == 1:
                adaptation = b"\x00"
            else:
                adaptation = bytes([stuffing - 1, 0x00]) + b"\xff" * (stuffing - 2)
    packet = bytes(header) + adaptation + payload
    return packet[:PACKET_SIZE].ljust(PACKET_SIZE, b"\xff")


class _Muxer:
    def __init__(self) -> None:
        self.counters: dict[int, int] = {}
        self.packets: list[bytes] = []

    def _next(self, pid: int) -> int:
        value = self.counters.get(pid, 15)
        value = (value + 1) % 16
        self.counters[pid] = value
        return value

    def psi(self, pid: int, section: bytes) -> None:
        payload = b"\x00" + section
        self.packets.append(_ts_packet(pid, payload, unit_start=True, counter=self._next(pid)))

    def pes(
        self,
        pid: int,
        data: bytes,
        pts: int | None,
        *,
        stream_id: int,
        pcr: int | None = None,
        skip_counter: bool = False,
    ) -> None:
        header = bytearray(b"\x00\x00\x01")
        header.append(stream_id)
        if pts is not None:
            pts_bytes = bytes(
                [
                    0x21 | ((pts >> 29) & 0x0E),
                    (pts >> 22) & 0xFF,
                    0x01 | ((pts >> 14) & 0xFE),
                    (pts >> 7) & 0xFF,
                    0x01 | ((pts << 1) & 0xFE),
                ]
            )
            optional = bytes([0x80, 0x80, 5]) + pts_bytes
        else:
            optional = bytes([0x80, 0x00, 0])
        body = bytes(optional) + data
        packet_length = len(body)
        header += (packet_length if packet_length < 65536 else 0).to_bytes(2, "big")
        payload = bytes(header) + body

        first = True
        offset = 0
        skipped = False
        while offset < len(payload):
            counter = self._next(pid)
            if skip_counter and not first and not skipped:
                # Deliberately drop a counter step so a continuity-counter error is injected.
                # It has to land after the first packet on this PID, because the first packet
                # has no predecessor to be discontinuous with.
                counter = self._next(pid)
                skipped = True
            chunk_space = PACKET_SIZE - 4 - (8 if (first and pcr is not None) else 0)
            chunk = payload[offset : offset + chunk_space]
            self.packets.append(
                _ts_packet(
                    pid,
                    chunk,
                    unit_start=first,
                    counter=counter,
                    pcr=pcr if first else None,
                )
            )
            offset += len(chunk)
            first = False

    def bytes(self) -> bytes:
        return b"".join(self.packets)


def pat_section() -> bytes:
    body = b"\x00\x01\xc1\x00\x00" + (1).to_bytes(2, "big") + (0xE000 | PMT_PID).to_bytes(2, "big")
    return _section(0x00, body)


def pmt_section(
    *,
    with_audio: bool = True,
    with_video: bool = True,
    audio_type: int = STREAM_TYPE_AAC,
    with_scte35: bool = False,
) -> bytes:
    body = bytearray()
    body += (1).to_bytes(2, "big")
    body += b"\xc1\x00\x00"
    # The PCR rides on whichever elementary stream the segment actually carries. A demuxed
    # audio rendition has no video PID at all, so it cannot be the video one.
    body += (0xE000 | (VIDEO_PID if with_video else AUDIO_PID)).to_bytes(2, "big")
    body += (0xF000).to_bytes(2, "big")
    if with_video:
        body += bytes([STREAM_TYPE_H264]) + (0xE000 | VIDEO_PID).to_bytes(2, "big") + b"\xf0\x00"
    if with_audio:
        body += bytes([audio_type]) + (0xE000 | AUDIO_PID).to_bytes(2, "big") + b"\xf0\x00"
    if with_scte35:
        body += bytes([STREAM_TYPE_SCTE35]) + (0xE000 | SCTE35_PID).to_bytes(2, "big") + b"\xf0\x00"
    return _section(0x02, bytes(body))


def build_sps(
    *,
    width: int = 1280,
    height: int = 720,
    max_num_ref_frames: int = 4,
    profile_idc: int = 100,
    level_idc: int = 31,
) -> bytes:
    """Build an SPS RBSP the project's own decoder round-trips."""
    bits: list[int] = []

    def u(value: int, count: int) -> None:
        for shift in range(count - 1, -1, -1):
            bits.append((value >> shift) & 1)

    def ue(value: int) -> None:
        value += 1
        length = value.bit_length()
        for _ in range(length - 1):
            bits.append(0)
        u(value, length)

    u(profile_idc, 8)
    u(0, 8)  # constraint flags
    u(level_idc, 8)
    ue(0)  # sps id
    if profile_idc in (100, 110, 122, 244):
        ue(1)  # chroma_format_idc = 4:2:0
        ue(0)  # bit_depth_luma_minus8
        ue(0)  # bit_depth_chroma_minus8
        u(0, 1)  # qpprime
        u(0, 1)  # scaling matrix absent
    ue(0)  # log2_max_frame_num_minus4
    ue(0)  # pic_order_cnt_type
    ue(0)  # log2_max_pic_order_cnt_lsb_minus4
    ue(max_num_ref_frames)
    u(0, 1)  # gaps_in_frame_num_value_allowed_flag
    ue(width // 16 - 1)
    ue(height // 16 - 1)
    u(1, 1)  # frame_mbs_only_flag
    u(1, 1)  # direct_8x8_inference_flag
    u(0, 1)  # frame_cropping_flag
    u(1, 1)  # vui_parameters_present_flag
    u(0, 1)  # aspect_ratio_info_present_flag
    u(0, 1)  # overscan_info_present_flag
    u(0, 1)  # video_signal_type_present_flag
    u(0, 1)  # chroma_loc_info_present_flag
    u(1, 1)  # timing_info_present_flag
    u(1000, 32)  # num_units_in_tick
    u(60000, 32)  # time_scale -> 30 fps
    u(1, 1)  # fixed_frame_rate_flag
    u(0, 1)  # nal_hrd absent
    u(0, 1)  # vcl_hrd absent
    u(0, 1)  # pic_struct_present_flag
    u(0, 1)  # bitstream_restriction_flag
    bits.append(1)  # rbsp_stop_one_bit
    while len(bits) % 8:
        bits.append(0)

    out = bytearray()
    for index in range(0, len(bits), 8):
        byte = 0
        for bit in bits[index : index + 8]:
            byte = (byte << 1) | bit
        out.append(byte)
    return bytes(out)


def _annexb(nal_type: int, payload: bytes) -> bytes:
    return b"\x00\x00\x00\x01" + bytes([nal_type & 0x1F]) + payload


def adts_frames(
    *, sample_rate_index: int = 3, channel_config: int = 2, frames: int = 40, aot: int = 2
) -> bytes:
    """Build a run of ADTS frames with a chosen configuration."""
    out = bytearray()
    payload = b"\x21\x00" + b"\x00" * 30
    frame_length = 7 + len(payload)
    for _ in range(frames):
        header = bytearray(7)
        header[0] = 0xFF
        header[1] = 0xF1
        header[2] = ((aot - 1) << 6) | (sample_rate_index << 2) | ((channel_config >> 2) & 0x01)
        header[3] = ((channel_config & 0x03) << 6) | ((frame_length >> 11) & 0x03)
        header[4] = (frame_length >> 3) & 0xFF
        header[5] = ((frame_length & 0x07) << 5) | 0x1F
        header[6] = 0xFC
        out += bytes(header) + payload
    return bytes(out)


@dataclass
class SegmentSpec:
    """Every fault a synthetic segment can carry."""

    duration: float = 6.0
    pts_offset_s: float = 0.0
    with_audio: bool = True
    # False builds an audio-only segment: no video PID in the PMT and no video PES. That is
    # what a demuxed audio rendition publishes, and the only way to produce an A/V skew that
    # no single segment carries.
    with_video: bool = True
    audio_pts_offset_ms: float = 0.0
    start_with_idr: bool = True
    include_sps: bool = True
    max_num_ref_frames: int = 4
    width: int = 1280
    height: int = 720
    audio_sample_rate_index: int = 3
    audio_channel_config: int = 2
    audio_aot: int = 2
    tiny: bool = False
    broken_sync: bool = False
    missing_pat: bool = False
    missing_pmt: bool = False
    continuity_error: bool = False
    padding_bytes: int = 120_000
    scte35_section: bytes | None = None


def build_ts_segment(spec: SegmentSpec) -> bytes:
    """Mux one synthetic TS segment according to ``spec``."""
    muxer = _Muxer()
    if not spec.missing_pat:
        muxer.psi(PAT_PID, pat_section())
    if not spec.missing_pmt:
        muxer.psi(
            PMT_PID,
            pmt_section(
                with_audio=spec.with_audio,
                with_video=spec.with_video,
                with_scte35=spec.scte35_section is not None,
            ),
        )

    video_pts = int(spec.pts_offset_s * PTS_HZ)
    nals = b""
    if spec.include_sps:
        nals += _annexb(
            7,
            build_sps(
                width=spec.width,
                height=spec.height,
                max_num_ref_frames=spec.max_num_ref_frames,
            ),
        )
        nals += _annexb(8, b"\xce\x3c\x80")  # PPS
    nals += _annexb(5 if spec.start_with_idr else 1, b"\x88\x84\x00" + b"\x00" * 64)
    filler = 0 if spec.tiny else spec.padding_bytes
    nals += _annexb(1, b"\x9a" * filler) if filler else b""

    if spec.with_video:
        muxer.pes(
            VIDEO_PID,
            nals,
            video_pts,
            stream_id=0xE0,
            pcr=video_pts,
            skip_counter=spec.continuity_error,
        )

    if spec.with_audio:
        audio_pts = video_pts + int(spec.audio_pts_offset_ms / 1000.0 * PTS_HZ)
        frames = max(1, int(spec.duration * 43))
        muxer.pes(
            AUDIO_PID,
            adts_frames(
                sample_rate_index=spec.audio_sample_rate_index,
                channel_config=spec.audio_channel_config,
                aot=spec.audio_aot,
                frames=min(frames, 300),
            ),
            audio_pts,
            stream_id=0xC0,
            pcr=audio_pts if not spec.with_video else None,
        )

    # A second PES so last_pts lands at the end of the declared duration.
    end_pts = video_pts + int(spec.duration * PTS_HZ)
    if spec.with_video:
        muxer.pes(VIDEO_PID, _annexb(1, b"\x9a" * 64), end_pts, stream_id=0xE0)
    if spec.with_audio:
        muxer.pes(
            AUDIO_PID,
            adts_frames(
                sample_rate_index=spec.audio_sample_rate_index,
                channel_config=spec.audio_channel_config,
                aot=spec.audio_aot,
                frames=2,
            ),
            end_pts + int(spec.audio_pts_offset_ms / 1000.0 * PTS_HZ),
            stream_id=0xC0,
        )

    if spec.scte35_section:
        muxer.psi(SCTE35_PID, spec.scte35_section)

    data = muxer.bytes()
    if spec.broken_sync:
        data = b"\x00\x00" + data[2:]
    return data


@dataclass
class PlaylistSpec:
    """Everything needed to render a media playlist, faults included."""

    target_duration: int = 6
    media_sequence: int = 0
    discontinuity_sequence: int = 0
    segment_count: int = 6
    segment_duration: float = 6.0
    segment_prefix: str = "seg"
    endlist: bool = False
    version: int = 3
    program_date_time: str | None = "2026-09-11T10:00:00.000Z"
    discontinuity_at: set[int] = field(default_factory=set)
    durations: list[float] | None = None
    cue_out_at: int | None = None
    cue_in_at: int | None = None
    cue_out_duration: float = 30.0
    map_uri: str | None = None
    gap_at: set[int] = field(default_factory=set)
    omit_target_duration: bool = False


def render_media_playlist(spec: PlaylistSpec) -> str:
    lines = ["#EXTM3U", f"#EXT-X-VERSION:{spec.version}"]
    if not spec.omit_target_duration:
        lines.append(f"#EXT-X-TARGETDURATION:{spec.target_duration}")
    lines.append(f"#EXT-X-MEDIA-SEQUENCE:{spec.media_sequence}")
    if spec.discontinuity_sequence:
        lines.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{spec.discontinuity_sequence}")
    if spec.map_uri:
        lines.append(f'#EXT-X-MAP:URI="{spec.map_uri}"')

    for index in range(spec.segment_count):
        msn = spec.media_sequence + index
        if index in spec.discontinuity_at:
            lines.append("#EXT-X-DISCONTINUITY")
        if spec.cue_out_at is not None and index == spec.cue_out_at:
            lines.append(f"#EXT-X-CUE-OUT:DURATION={spec.cue_out_duration}")
        if spec.cue_in_at is not None and index == spec.cue_in_at:
            lines.append("#EXT-X-CUE-IN")
        if spec.program_date_time and index == 0:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{spec.program_date_time}")
        if index in spec.gap_at:
            lines.append("#EXT-X-GAP")
        duration = spec.durations[index] if spec.durations else spec.segment_duration
        lines.append(f"#EXTINF:{duration:.3f},")
        lines.append(f"{spec.segment_prefix}{msn}.ts")

    if spec.endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


@dataclass
class VariantSpec:
    name: str
    bandwidth: int
    resolution: str | None = "1280x720"
    codecs: str | None = "avc1.64001f,mp4a.40.2"
    frame_rate: float | None = 30.0
    average_bandwidth: int | None = None
    audio_group: str | None = None
    uri: str | None = None


def render_master_playlist(
    variants: list[VariantSpec],
    *,
    audio_renditions: list[tuple[str, str, str]] | None = None,
    independent_segments: bool = True,
    version: int | None = None,
) -> str:
    # EXT-X-INDEPENDENT-SEGMENTS requires version 6, so the default follows the tags used.
    if version is None:
        version = 6 if independent_segments else 3
    lines = ["#EXTM3U", f"#EXT-X-VERSION:{version}"]
    if independent_segments:
        lines.append("#EXT-X-INDEPENDENT-SEGMENTS")
    for group_id, name, uri in audio_renditions or []:
        entry = (
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="{group_id}",NAME="{name}",'
            f'LANGUAGE="en",DEFAULT=YES,AUTOSELECT=YES'
        )
        # An empty URI renders the entry without one, which is how RFC 8216 §4.3.4.2.1
        # spells audio that rides in the video segments. Such a rung is muxed, and the
        # layout detector has to say so despite the AUDIO attribute.
        if uri:
            entry += f',URI="{uri}"'
        lines.append(entry)
    for variant in variants:
        attrs = [f"BANDWIDTH={variant.bandwidth}"]
        if variant.average_bandwidth:
            attrs.append(f"AVERAGE-BANDWIDTH={variant.average_bandwidth}")
        if variant.resolution:
            attrs.append(f"RESOLUTION={variant.resolution}")
        if variant.frame_rate:
            attrs.append(f"FRAME-RATE={variant.frame_rate}")
        if variant.codecs:
            attrs.append(f'CODECS="{variant.codecs}"')
        if variant.audio_group:
            attrs.append(f'AUDIO="{variant.audio_group}"')
        lines.append("#EXT-X-STREAM-INF:" + ",".join(attrs))
        lines.append(variant.uri or f"{variant.name}.m3u8")
    return "\n".join(lines) + "\n"
