"""H.264 SPS decoder.

Decoded in full because two of the fields drive Tizen findings: `max_num_ref_frames`
(a ladder whose rungs disagree forces a DPB reallocation on every ABR switch) and the
frame-cropping values (a rung whose coded size differs from its declared RESOLUTION).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.media.bitreader import BitReader

PROFILE_NAMES = {
    66: "Baseline",
    77: "Main",
    88: "Extended",
    100: "High",
    110: "High 10",
    122: "High 4:2:2",
    244: "High 4:4:4 Predictive",
}

CHROMA_NAMES = {0: "monochrome", 1: "4:2:0", 2: "4:2:2", 3: "4:4:4"}


@dataclass(slots=True)
class H264Sps:
    profile_idc: int
    level_idc: int
    constraint_flags: int
    seq_parameter_set_id: int
    chroma_format_idc: int
    bit_depth_luma: int
    bit_depth_chroma: int
    log2_max_frame_num: int
    pic_order_cnt_type: int
    max_num_ref_frames: int
    width: int
    height: int
    frame_mbs_only_flag: int
    crop: tuple[int, int, int, int] = (0, 0, 0, 0)
    vui_present: bool = False
    time_scale: int | None = None
    num_units_in_tick: int | None = None
    fixed_frame_rate: bool | None = None
    raw_len: int = 0
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def profile_name(self) -> str:
        return PROFILE_NAMES.get(self.profile_idc, f"profile_idc={self.profile_idc}")

    @property
    def level(self) -> str:
        return f"{self.level_idc // 10}.{self.level_idc % 10}"

    @property
    def chroma_format(self) -> str:
        return CHROMA_NAMES.get(self.chroma_format_idc, str(self.chroma_format_idc))

    @property
    def progressive(self) -> bool:
        return self.frame_mbs_only_flag == 1

    @property
    def scan_type(self) -> str:
        return "progressive" if self.progressive else "interlaced"

    @property
    def frame_rate(self) -> float | None:
        if self.time_scale and self.num_units_in_tick:
            return self.time_scale / (2.0 * self.num_units_in_tick)
        return None

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def dpb_footprint(self) -> int:
        """Frames held x picture area — the number a decoder must allocate for."""
        return self.max_num_ref_frames * self.width * self.height

    @property
    def codec_string(self) -> str:
        return f"avc1.{self.profile_idc:02x}{self.constraint_flags:02x}{self.level_idc:02x}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "codec": "h264",
            "profile": self.profile_name,
            "profile_idc": self.profile_idc,
            "level": self.level,
            "chroma_format": self.chroma_format,
            "bit_depth_luma": self.bit_depth_luma,
            "bit_depth_chroma": self.bit_depth_chroma,
            "max_num_ref_frames": self.max_num_ref_frames,
            "width": self.width,
            "height": self.height,
            "resolution": self.resolution,
            "scan_type": self.scan_type,
            "frame_rate": self.frame_rate,
            "codec_string": self.codec_string,
            "dpb_footprint": self.dpb_footprint,
        }


def _skip_scaling_list(reader: BitReader, size: int) -> None:
    last_scale = 8
    next_scale = 8
    for _ in range(size):
        if next_scale != 0:
            delta = reader.se()
            next_scale = (last_scale + delta + 256) % 256
        last_scale = next_scale if next_scale != 0 else last_scale


def parse_sps(nal_payload: bytes) -> H264Sps | None:
    """Decode an SPS NAL payload (the bytes after the 1-byte NAL header)."""
    if len(nal_payload) < 4:
        return None
    try:
        reader = BitReader(nal_payload)
        profile_idc = reader.u(8)
        constraint_flags = reader.u(8)
        level_idc = reader.u(8)
        sps_id = reader.ue()

        chroma_format_idc = 1
        bit_depth_luma = 8
        bit_depth_chroma = 8
        if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            chroma_format_idc = reader.ue()
            if chroma_format_idc == 3:
                reader.u1()  # separate_colour_plane_flag
            bit_depth_luma = reader.ue() + 8
            bit_depth_chroma = reader.ue() + 8
            reader.u1()  # qpprime_y_zero_transform_bypass_flag
            if reader.flag():  # seq_scaling_matrix_present_flag
                count = 8 if chroma_format_idc != 3 else 12
                for i in range(count):
                    if reader.flag():
                        _skip_scaling_list(reader, 16 if i < 6 else 64)

        log2_max_frame_num = reader.ue() + 4
        pic_order_cnt_type = reader.ue()
        if pic_order_cnt_type == 0:
            reader.ue()  # log2_max_pic_order_cnt_lsb_minus4
        elif pic_order_cnt_type == 1:
            reader.u1()  # delta_pic_order_always_zero_flag
            reader.se()  # offset_for_non_ref_pic
            reader.se()  # offset_for_top_to_bottom_field
            for _ in range(reader.ue()):
                reader.se()

        max_num_ref_frames = reader.ue()
        reader.u1()  # gaps_in_frame_num_value_allowed_flag
        pic_width_in_mbs = reader.ue() + 1
        pic_height_in_map_units = reader.ue() + 1
        frame_mbs_only_flag = reader.u1()
        if frame_mbs_only_flag == 0:
            reader.u1()  # mb_adaptive_frame_field_flag
        reader.u1()  # direct_8x8_inference_flag

        crop = (0, 0, 0, 0)
        if reader.flag():  # frame_cropping_flag
            crop = (reader.ue(), reader.ue(), reader.ue(), reader.ue())

        width = pic_width_in_mbs * 16
        height = (2 - frame_mbs_only_flag) * pic_height_in_map_units * 16
        sub_width = 2 if chroma_format_idc in (1, 2) else 1
        sub_height = 2 if chroma_format_idc == 1 else 1
        if chroma_format_idc == 0:
            sub_width = sub_height = 1
        width -= (crop[0] + crop[1]) * sub_width
        height -= (crop[2] + crop[3]) * sub_height * (2 - frame_mbs_only_flag)

        sps = H264Sps(
            profile_idc=profile_idc,
            level_idc=level_idc,
            constraint_flags=constraint_flags,
            seq_parameter_set_id=sps_id,
            chroma_format_idc=chroma_format_idc,
            bit_depth_luma=bit_depth_luma,
            bit_depth_chroma=bit_depth_chroma,
            log2_max_frame_num=log2_max_frame_num,
            pic_order_cnt_type=pic_order_cnt_type,
            max_num_ref_frames=max_num_ref_frames,
            width=width,
            height=height,
            frame_mbs_only_flag=frame_mbs_only_flag,
            crop=crop,
            raw_len=len(nal_payload),
        )

        if reader.flag():  # vui_parameters_present_flag
            sps.vui_present = True
            _parse_vui(reader, sps)
        return sps
    except (EOFError, ValueError):
        return None


def _parse_vui(reader: BitReader, sps: H264Sps) -> None:
    if reader.flag():  # aspect_ratio_info_present_flag
        aspect_ratio_idc = reader.u(8)
        if aspect_ratio_idc == 255:
            reader.u(16)
            reader.u(16)
    if reader.flag():  # overscan_info_present_flag
        reader.u1()
    if reader.flag():  # video_signal_type_present_flag
        reader.u(3)
        reader.u1()
        if reader.flag():  # colour_description_present_flag
            sps.extras["colour_primaries"] = reader.u(8)
            sps.extras["transfer_characteristics"] = reader.u(8)
            sps.extras["matrix_coefficients"] = reader.u(8)
    if reader.flag():  # chroma_loc_info_present_flag
        reader.ue()
        reader.ue()
    if reader.flag():  # timing_info_present_flag
        sps.num_units_in_tick = reader.u(32)
        sps.time_scale = reader.u(32)
        sps.fixed_frame_rate = reader.flag()


def iter_nal_units(data: bytes) -> list[tuple[int, bytes]]:
    """Split an Annex-B byte stream into ``(nal_type, payload)`` pairs."""
    units: list[tuple[int, bytes]] = []
    index = 0
    length = len(data)
    starts: list[int] = []
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
        end = max(start, end)
        chunk = data[start:end]
        if not chunk:
            continue
        units.append((chunk[0] & 0x1F, chunk[1:]))
    return units
