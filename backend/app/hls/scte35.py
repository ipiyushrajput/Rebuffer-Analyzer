"""SCTE-35 decoding.

Two sources are decoded and compared: sections carried in-band on the TS PID whose
stream_type is 0x86, and the `EXT-X-CUE-OUT` / `EXT-X-CUE-IN` / `EXT-X-DATERANGE` tags in the
playlist. A cue present in one and absent in the other is a finding.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from typing import Any

SPLICE_NULL = 0x00
SPLICE_SCHEDULE = 0x04
SPLICE_INSERT = 0x05
TIME_SIGNAL = 0x06
BANDWIDTH_RESERVATION = 0x07
PRIVATE_COMMAND = 0xFF

COMMAND_NAMES = {
    SPLICE_NULL: "splice_null",
    SPLICE_SCHEDULE: "splice_schedule",
    SPLICE_INSERT: "splice_insert",
    TIME_SIGNAL: "time_signal",
    BANDWIDTH_RESERVATION: "bandwidth_reservation",
    PRIVATE_COMMAND: "private_command",
}

TICKS_PER_SECOND = 90000.0


@dataclass(slots=True)
class SpliceInfo:
    command_type: int
    command_name: str
    pts_adjustment: int = 0
    splice_event_id: int | None = None
    out_of_network: bool | None = None
    splice_time_pts: int | None = None
    break_duration_s: float | None = None
    segmentation_type_id: int | None = None
    segmentation_upid: str | None = None
    raw_hex: str = ""
    error: str | None = None
    descriptors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_ad_start(self) -> bool:
        if self.out_of_network is True:
            return True
        return self.segmentation_type_id in (0x30, 0x32, 0x34, 0x36, 0x44)

    @property
    def is_ad_end(self) -> bool:
        if self.out_of_network is False:
            return True
        return self.segmentation_type_id in (0x31, 0x33, 0x35, 0x37, 0x45)

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command_name,
            "command_type": self.command_type,
            "splice_event_id": self.splice_event_id,
            "out_of_network": self.out_of_network,
            "splice_time_pts": self.splice_time_pts,
            "break_duration_s": self.break_duration_s,
            "segmentation_type_id": self.segmentation_type_id,
            "segmentation_upid": self.segmentation_upid,
            "is_ad_start": self.is_ad_start,
            "is_ad_end": self.is_ad_end,
            "error": self.error,
        }


def decode_section(section: bytes) -> SpliceInfo | None:
    """Decode a `splice_info_section`."""
    if len(section) < 14 or section[0] != 0xFC:
        return None
    try:
        pts_adjustment = (
            (section[4] & 0x01) << 32
            | section[5] << 24
            | section[6] << 16
            | section[7] << 8
            | section[8]
        )
        splice_command_length = ((section[11] & 0x0F) << 8) | section[12]
        command_type = section[13]
        info = SpliceInfo(
            command_type=command_type,
            command_name=COMMAND_NAMES.get(command_type, f"0x{command_type:02x}"),
            pts_adjustment=pts_adjustment,
            raw_hex=binascii.hexlify(section[:32]).decode(),
        )
        body = section[14:]

        if command_type == SPLICE_INSERT:
            _decode_splice_insert(body, info)
        elif command_type == TIME_SIGNAL:
            info.splice_time_pts = _decode_splice_time(body)

        descriptor_start = 14 + (splice_command_length if splice_command_length != 0xFFF else 0)
        _decode_descriptors(section, descriptor_start, info)
        return info
    except (IndexError, ValueError) as exc:
        return SpliceInfo(
            command_type=-1,
            command_name="undecodable",
            error=f"splice_info_section is not decodable: {exc}",
            raw_hex=binascii.hexlify(section[:32]).decode(),
        )


def _decode_splice_time(body: bytes) -> int | None:
    if not body:
        return None
    if not body[0] & 0x80:
        return None
    if len(body) < 5:
        return None
    return (body[0] & 0x01) << 32 | body[1] << 24 | body[2] << 16 | body[3] << 8 | body[4]


def _decode_splice_insert(body: bytes, info: SpliceInfo) -> None:
    if len(body) < 5:
        return
    info.splice_event_id = int.from_bytes(body[0:4], "big")
    cancel = bool(body[4] & 0x80)
    if cancel:
        return
    flags = body[5]
    info.out_of_network = bool(flags & 0x80)
    program_splice = bool(flags & 0x40)
    duration_flag = bool(flags & 0x20)
    offset = 6
    if program_splice:
        time = _decode_splice_time(body[offset:])
        info.splice_time_pts = time
        offset += 5 if time is not None else 1
    if duration_flag and len(body) >= offset + 5:
        duration_ticks = (
            (body[offset] & 0x01) << 32
            | body[offset + 1] << 24
            | body[offset + 2] << 16
            | body[offset + 3] << 8
            | body[offset + 4]
        )
        info.break_duration_s = duration_ticks / TICKS_PER_SECOND


def _decode_descriptors(section: bytes, start: int, info: SpliceInfo) -> None:
    if start + 2 > len(section):
        return
    loop_length = int.from_bytes(section[start : start + 2], "big")
    index = start + 2
    end = min(index + loop_length, len(section) - 4)
    while index + 2 <= end:
        tag = section[index]
        length = section[index + 1]
        payload = section[index + 2 : index + 2 + length]
        if tag == 0x02 and len(payload) >= 15:  # segmentation_descriptor
            cursor = 4  # splice_descriptor identifier "CUEI"
            cursor += 4  # segmentation_event_id
            cancel = bool(payload[cursor] & 0x80)
            cursor += 1
            if not cancel and cursor < len(payload):
                flags = payload[cursor]
                cursor += 1
                delivery_not_restricted = bool(flags & 0x20)
                if not delivery_not_restricted:
                    cursor += 1
                if not (flags & 0x40):  # program_segmentation_flag clear
                    cursor += 1
                if flags & 0x80 and cursor + 5 <= len(payload):  # duration flag
                    ticks = int.from_bytes(payload[cursor : cursor + 5], "big")
                    info.break_duration_s = ticks / TICKS_PER_SECOND
                    cursor += 5
                if cursor + 2 <= len(payload):
                    upid_length = payload[cursor + 1]
                    cursor += 2
                    upid = payload[cursor : cursor + upid_length]
                    info.segmentation_upid = binascii.hexlify(upid).decode()
                    cursor += upid_length
                if cursor < len(payload):
                    info.segmentation_type_id = payload[cursor]
        info.descriptors.append({"tag": tag, "length": length})
        index += 2 + length


def decode_base64(value: str) -> SpliceInfo | None:
    """Decode the base64 payload of `SCTE35-OUT` / `SCTE35-IN` daterange attributes."""
    try:
        return decode_section(base64.b64decode(value))
    except (binascii.Error, ValueError):
        return None


def decode_hex(value: str) -> SpliceInfo | None:
    text = value[2:] if value.lower().startswith("0x") else value
    try:
        return decode_section(binascii.unhexlify(text))
    except (binascii.Error, ValueError):
        return None


@dataclass(slots=True)
class CueWindow:
    """One `CUE-OUT` .. `CUE-IN` pair observed in a playlist."""

    start_msn: int
    declared_duration_s: float | None
    end_msn: int | None = None
    delivered_duration_s: float = 0.0
    segment_count: int = 0
    closed: bool = False
    discontinuity_at_out: bool = False
    discontinuity_at_in: bool = False

    @property
    def duration_delta_s(self) -> float | None:
        if self.declared_duration_s is None:
            return None
        return self.delivered_duration_s - self.declared_duration_s


def extract_cue_windows(playlist: Any) -> list[CueWindow]:
    """Walk a parsed media playlist and pair every CUE-OUT with its CUE-IN."""
    windows: list[CueWindow] = []
    current: CueWindow | None = None
    for segment in playlist.segments:
        if segment.cue_out is not None and current is None:
            current = CueWindow(
                start_msn=segment.msn,
                declared_duration_s=segment.cue_out or None,
                discontinuity_at_out=segment.discontinuity_before,
            )
            windows.append(current)
        if current is not None and not current.closed:
            current.delivered_duration_s += segment.duration
            current.segment_count += 1
        if segment.cue_in and current is not None:
            current.end_msn = segment.msn
            current.closed = True
            current.discontinuity_at_in = segment.discontinuity_before
            current = None
    return windows
