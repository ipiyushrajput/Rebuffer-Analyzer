"""fMP4 / CMAF box parsing.

Reads the init segment for track configuration and each media segment for `tfdt` base
decode time and `trun` sample durations, so the same PTS-continuity and duration rules that
run on TS also run on CMAF.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

CONTAINER_BOXES = frozenset(
    {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"moof", b"traf", b"mvex", b"edts"}
)


@dataclass(slots=True)
class Box:
    type: bytes
    size: int
    offset: int
    payload: bytes

    @property
    def name(self) -> str:
        return self.type.decode("latin-1")


def iter_boxes(data: bytes, start: int = 0, end: int | None = None) -> Iterator[Box]:
    """Walk the boxes at one nesting level."""
    end = len(data) if end is None else end
    offset = start
    while offset + 8 <= end:
        size = struct.unpack_from(">I", data, offset)[0]
        box_type = data[offset + 4 : offset + 8]
        header = 8
        if size == 1:
            if offset + 16 > end:
                return
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            return
        yield Box(
            type=box_type,
            size=size,
            offset=offset,
            payload=data[offset + header : offset + size],
        )
        offset += size


def find_boxes(data: bytes, path: tuple[bytes, ...]) -> list[Box]:
    """Find every box matching a nested path, e.g. ``(b"moov", b"trak", b"mdia")``."""
    results: list[Box] = []

    def walk(payload: bytes, remaining: tuple[bytes, ...]) -> None:
        target = remaining[0]
        for box in iter_boxes(payload):
            if box.type != target:
                continue
            if len(remaining) == 1:
                results.append(box)
            else:
                walk(box.payload, remaining[1:])

    walk(data, path)
    return results


@dataclass(slots=True)
class Fmp4Track:
    track_id: int
    timescale: int
    handler: str = ""
    codec: str = ""
    width: int | None = None
    height: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    sps: dict[str, Any] | None = None
    default_sample_duration: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "timescale": self.timescale,
            "handler": self.handler,
            "codec": self.codec,
            "width": self.width,
            "height": self.height,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "sps": self.sps,
        }


@dataclass(slots=True)
class Fmp4Analysis:
    is_init: bool = False
    has_moof: bool = False
    has_mdat: bool = False
    tracks: dict[int, Fmp4Track] = field(default_factory=dict)
    base_media_decode_time: dict[int, int] = field(default_factory=dict)
    sample_duration_sum: dict[int, int] = field(default_factory=dict)
    sample_count: dict[int, int] = field(default_factory=dict)
    styp: str | None = None
    error: str | None = None

    def duration_s(self, track_id: int, timescale: int | None = None) -> float | None:
        scale = timescale or (self.tracks[track_id].timescale if track_id in self.tracks else None)
        if not scale:
            return None
        return self.sample_duration_sum.get(track_id, 0) / scale

    def start_s(self, track_id: int, timescale: int | None = None) -> float | None:
        scale = timescale or (self.tracks[track_id].timescale if track_id in self.tracks else None)
        base = self.base_media_decode_time.get(track_id)
        if not scale or base is None:
            return None
        return base / scale

    def as_dict(self) -> dict[str, Any]:
        return {
            "container": "fmp4",
            "is_init": self.is_init,
            "has_moof": self.has_moof,
            "has_mdat": self.has_mdat,
            "tracks": [t.as_dict() for t in self.tracks.values()],
            "tfdt": self.base_media_decode_time,
            "sample_counts": self.sample_count,
            "error": self.error,
        }


def looks_like_fmp4(data: bytes) -> bool:
    for box in iter_boxes(data[: min(len(data), 4096)]):
        if box.type in (b"ftyp", b"styp", b"moov", b"moof", b"sidx", b"emsg"):
            return True
        break
    return False


def parse_init(data: bytes) -> Fmp4Analysis:
    """Parse an `EXT-X-MAP` init segment: track ids, timescales and codec configuration."""
    analysis = Fmp4Analysis(is_init=True)
    if not data:
        analysis.error = "Init segment body is empty"
        return analysis

    for trak in find_boxes(data, (b"moov", b"trak")):
        track_id = None
        for tkhd in find_boxes(trak.payload, (b"tkhd",)):
            version = tkhd.payload[0]
            track_id = struct.unpack_from(">I", tkhd.payload, 20 if version == 1 else 12)[0]
        if track_id is None:
            continue
        timescale = 0
        for mdhd in find_boxes(trak.payload, (b"mdia", b"mdhd")):
            version = mdhd.payload[0]
            timescale = struct.unpack_from(">I", mdhd.payload, 20 if version == 1 else 12)[0]
        track = Fmp4Track(track_id=track_id, timescale=timescale)
        for hdlr in find_boxes(trak.payload, (b"mdia", b"hdlr")):
            track.handler = hdlr.payload[8:12].decode("latin-1", errors="replace")
        for stsd in find_boxes(trak.payload, (b"mdia", b"minf", b"stbl", b"stsd")):
            _parse_stsd(stsd.payload, track)
        analysis.tracks[track_id] = track
    return analysis


def _parse_stsd(payload: bytes, track: Fmp4Track) -> None:
    from app.media import h264_sps, hevc_sps

    if len(payload) < 8:
        return
    for entry in iter_boxes(payload, start=8):
        track.codec = entry.name
        body = entry.payload
        if entry.type in (b"avc1", b"avc3", b"hvc1", b"hev1", b"dvh1", b"dvhe") and len(body) >= 78:
            track.width = struct.unpack_from(">H", body, 24)[0]
            track.height = struct.unpack_from(">H", body, 26)[0]
            for config in iter_boxes(body, start=78):
                if config.type == b"avcC":
                    raw = _sps_from_avcc(config.payload)
                    parsed = h264_sps.parse_sps(raw) if raw else None
                    track.sps = parsed.as_dict() if parsed else None
                elif config.type == b"hvcC":
                    raw = _sps_from_hvcc(config.payload)
                    parsed_hevc = hevc_sps.parse_sps(raw) if raw else None
                    track.sps = parsed_hevc.as_dict() if parsed_hevc else None
        elif entry.type in (b"mp4a", b"ac-3", b"ec-3", b"ac-4") and len(body) >= 28:
            track.channels = struct.unpack_from(">H", body, 16)[0]
            track.sample_rate = struct.unpack_from(">I", body, 24)[0] >> 16
        break


def _sps_from_avcc(payload: bytes) -> bytes | None:
    if len(payload) < 7:
        return None
    count = payload[5] & 0x1F
    offset = 6
    for _ in range(count):
        if offset + 2 > len(payload):
            return None
        length = struct.unpack_from(">H", payload, offset)[0]
        nal = payload[offset + 2 : offset + 2 + length]
        if nal:
            return nal[1:]
        offset += 2 + length
    return None


def _sps_from_hvcc(payload: bytes) -> bytes | None:
    if len(payload) < 23:
        return None
    num_arrays = payload[22]
    offset = 23
    for _ in range(num_arrays):
        if offset + 3 > len(payload):
            return None
        nal_type = payload[offset] & 0x3F
        num_nalus = struct.unpack_from(">H", payload, offset + 1)[0]
        offset += 3
        for _ in range(num_nalus):
            if offset + 2 > len(payload):
                return None
            length = struct.unpack_from(">H", payload, offset)[0]
            nal = payload[offset + 2 : offset + 2 + length]
            offset += 2 + length
            if nal_type == 33 and len(nal) > 2:
                return nal[2:]
    return None


def parse_segment(data: bytes) -> Fmp4Analysis:
    """Parse a media segment: `styp`, `moof`/`tfdt`/`trun`, `mdat`."""
    analysis = Fmp4Analysis()
    if not data:
        analysis.error = "Segment body is empty"
        return analysis

    for box in iter_boxes(data):
        if box.type == b"styp":
            analysis.styp = box.payload[:4].decode("latin-1", errors="replace")
        elif box.type == b"mdat":
            analysis.has_mdat = True
        elif box.type == b"moov":
            init = parse_init(data)
            analysis.tracks.update(init.tracks)
            analysis.is_init = True
        elif box.type == b"moof":
            analysis.has_moof = True
            for traf in iter_boxes(box.payload):
                if traf.type != b"traf":
                    continue
                track_id = 0
                default_duration = 0
                for child in iter_boxes(traf.payload):
                    if child.type == b"tfhd":
                        track_id, default_duration = _parse_tfhd(child.payload)
                    elif child.type == b"tfdt":
                        version = child.payload[0]
                        base = (
                            struct.unpack_from(">Q", child.payload, 4)[0]
                            if version == 1
                            else struct.unpack_from(">I", child.payload, 4)[0]
                        )
                        analysis.base_media_decode_time.setdefault(track_id, base)
                    elif child.type == b"trun":
                        count, total = _parse_trun(child.payload, default_duration)
                        analysis.sample_count[track_id] = (
                            analysis.sample_count.get(track_id, 0) + count
                        )
                        analysis.sample_duration_sum[track_id] = (
                            analysis.sample_duration_sum.get(track_id, 0) + total
                        )
    return analysis


def _parse_tfhd(payload: bytes) -> tuple[int, int]:
    flags = int.from_bytes(payload[1:4], "big")
    track_id = struct.unpack_from(">I", payload, 4)[0]
    offset = 8
    if flags & 0x000001:
        offset += 8  # base_data_offset
    if flags & 0x000002:
        offset += 4  # sample_description_index
    default_duration = 0
    if flags & 0x000008:
        default_duration = struct.unpack_from(">I", payload, offset)[0]
    return track_id, default_duration


def _parse_trun(payload: bytes, default_duration: int) -> tuple[int, int]:
    flags = int.from_bytes(payload[1:4], "big")
    sample_count = struct.unpack_from(">I", payload, 4)[0]
    offset = 8
    if flags & 0x000001:
        offset += 4  # data_offset
    if flags & 0x000004:
        offset += 4  # first_sample_flags
    per_sample = 0
    if flags & 0x000100:
        per_sample += 4
    if flags & 0x000200:
        per_sample += 4
    if flags & 0x000400:
        per_sample += 4
    if flags & 0x000800:
        per_sample += 4

    total = 0
    if flags & 0x000100:
        for _ in range(sample_count):
            if offset + 4 > len(payload):
                break
            total += struct.unpack_from(">I", payload, offset)[0]
            offset += per_sample
    else:
        total = sample_count * default_duration
    return sample_count, total
