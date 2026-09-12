"""HEVC VPS / SPS decoding and IRAP detection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.media.bitreader import BitReader

NAL_VPS = 32
NAL_SPS = 33
NAL_PPS = 34
NAL_AUD = 35
# IRAP: BLA_W_LP(16) .. RSV_IRAP_VCL23(23)
IRAP_RANGE = range(16, 24)

CHROMA_NAMES = {0: "monochrome", 1: "4:2:0", 2: "4:2:2", 3: "4:4:4"}


@dataclass(slots=True)
class HevcSps:
    profile_space: int
    tier_flag: int
    profile_idc: int
    level_idc: int
    chroma_format_idc: int
    width: int
    height: int
    bit_depth_luma: int
    bit_depth_chroma: int
    max_dec_pic_buffering: int
    max_num_reorder_pics: int
    general_profile_compatibility: int = 0
    time_scale: int | None = None
    num_units_in_tick: int | None = None

    @property
    def profile_name(self) -> str:
        return {1: "Main", 2: "Main 10", 3: "Main Still Picture"}.get(
            self.profile_idc, f"profile_idc={self.profile_idc}"
        )

    @property
    def tier(self) -> str:
        return "High" if self.tier_flag else "Main"

    @property
    def level(self) -> str:
        return f"{self.level_idc / 30:.1f}"

    @property
    def chroma_format(self) -> str:
        return CHROMA_NAMES.get(self.chroma_format_idc, str(self.chroma_format_idc))

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def frame_rate(self) -> float | None:
        if self.time_scale and self.num_units_in_tick:
            return self.time_scale / self.num_units_in_tick
        return None

    @property
    def dpb_footprint(self) -> int:
        return self.max_dec_pic_buffering * self.width * self.height

    @property
    def max_num_ref_frames(self) -> int:
        """Alias so cross-rung DPB comparison treats H.264 and HEVC the same way."""
        return self.max_dec_pic_buffering

    def as_dict(self) -> dict[str, Any]:
        return {
            "codec": "hevc",
            "profile": self.profile_name,
            "tier": self.tier,
            "level": self.level,
            "chroma_format": self.chroma_format,
            "bit_depth_luma": self.bit_depth_luma,
            "bit_depth_chroma": self.bit_depth_chroma,
            "width": self.width,
            "height": self.height,
            "resolution": self.resolution,
            "max_dec_pic_buffering": self.max_dec_pic_buffering,
            "max_num_reorder_pics": self.max_num_reorder_pics,
            "max_num_ref_frames": self.max_dec_pic_buffering,
            "frame_rate": self.frame_rate,
            "dpb_footprint": self.dpb_footprint,
        }


def _profile_tier_level(reader: BitReader, max_sub_layers: int) -> tuple[int, int, int, int, int]:
    profile_space = reader.u(2)
    tier_flag = reader.u(1)
    profile_idc = reader.u(5)
    compatibility = reader.u(32)
    reader.u(48)  # progressive/interlaced/non-packed/frame-only + 43 reserved + 1
    level_idc = reader.u(8)

    sub_layer_profile = []
    sub_layer_level = []
    for _ in range(max_sub_layers - 1):
        sub_layer_profile.append(reader.u(1))
        sub_layer_level.append(reader.u(1))
    if max_sub_layers > 1:
        for _ in range(max_sub_layers - 1, 8):
            reader.u(2)
    for i in range(max_sub_layers - 1):
        if sub_layer_profile[i]:
            reader.u(88)
        if sub_layer_level[i]:
            reader.u(8)
    return profile_space, tier_flag, profile_idc, compatibility, level_idc


def parse_sps(nal_payload: bytes) -> HevcSps | None:
    """Decode an HEVC SPS payload (bytes after the 2-byte NAL header)."""
    if len(nal_payload) < 8:
        return None
    try:
        reader = BitReader(nal_payload)
        reader.u(4)  # sps_video_parameter_set_id
        max_sub_layers = reader.u(3) + 1
        reader.u(1)  # sps_temporal_id_nesting_flag
        space, tier, profile, compat, level = _profile_tier_level(reader, max_sub_layers)
        reader.ue()  # sps_seq_parameter_set_id
        chroma_format_idc = reader.ue()
        if chroma_format_idc == 3:
            reader.u(1)
        width = reader.ue()
        height = reader.ue()
        if reader.flag():  # conformance_window_flag
            left, right, top, bottom = reader.ue(), reader.ue(), reader.ue(), reader.ue()
            sub_w = 2 if chroma_format_idc in (1, 2) else 1
            sub_h = 2 if chroma_format_idc == 1 else 1
            width -= (left + right) * sub_w
            height -= (top + bottom) * sub_h
        bit_depth_luma = reader.ue() + 8
        bit_depth_chroma = reader.ue() + 8
        reader.ue()  # log2_max_pic_order_cnt_lsb_minus4
        sub_layer_ordering_info = reader.u(1)
        start = 0 if sub_layer_ordering_info else max_sub_layers - 1
        max_dec_pic_buffering = 1
        max_num_reorder = 0
        for _ in range(start, max_sub_layers):
            max_dec_pic_buffering = reader.ue() + 1
            max_num_reorder = reader.ue()
            reader.ue()  # sps_max_latency_increase_plus1

        return HevcSps(
            profile_space=space,
            tier_flag=tier,
            profile_idc=profile,
            level_idc=level,
            general_profile_compatibility=compat,
            chroma_format_idc=chroma_format_idc,
            width=width,
            height=height,
            bit_depth_luma=bit_depth_luma,
            bit_depth_chroma=bit_depth_chroma,
            max_dec_pic_buffering=max_dec_pic_buffering,
            max_num_reorder_pics=max_num_reorder,
        )
    except (EOFError, ValueError):
        return None


def iter_nal_units(data: bytes) -> list[tuple[int, bytes]]:
    """Split an Annex-B HEVC stream into ``(nal_type, payload)`` pairs."""
    units: list[tuple[int, bytes]] = []
    starts: list[int] = []
    index = 0
    length = len(data)
    while index < length - 3:
        if data[index] == 0 and data[index + 1] == 0:
            if data[index + 2] == 1:
                starts.append(index + 3)
                index += 3
                continue
            if index < length - 4 and data[index + 2] == 0 and data[index + 3] == 1:
                starts.append(index + 4)
                index += 4
                continue
        index += 1
    for position, start in enumerate(starts):
        end = starts[position + 1] - 3 if position + 1 < len(starts) else length
        chunk = data[start : max(start, end)]
        if len(chunk) < 2:
            continue
        units.append(((chunk[0] >> 1) & 0x3F, chunk[2:]))
    return units


def has_irap(nal_types: list[int]) -> bool:
    return any(t in IRAP_RANGE for t in nal_types)
