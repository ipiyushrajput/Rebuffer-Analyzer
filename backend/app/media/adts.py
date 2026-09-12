"""AAC ADTS and raw-audio inspection.

An AAC configuration change between segments (AOT, sample rate, or channel configuration)
forces the Tizen audio decoder to reconfigure mid-stream, which stops playback. The config
is therefore read per segment and compared with the previous one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SAMPLE_RATES = [
    96000,
    88200,
    64000,
    48000,
    44100,
    32000,
    24000,
    22050,
    16000,
    12000,
    11025,
    8000,
    7350,
]

AOT_NAMES = {
    1: "AAC Main",
    2: "AAC-LC",
    3: "AAC-SSR",
    4: "AAC-LTP",
    5: "HE-AAC (SBR)",
    29: "HE-AACv2 (PS)",
}

CHANNEL_CONFIG_COUNT = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 8}

# Apple requires this PRIV frame at the head of every packed-audio segment; without it a
# player cannot map the elementary stream onto the playlist timeline.
ID3_PRIV_OWNER = b"com.apple.streaming.transportStreamTimestamp"


@dataclass(slots=True)
class AacConfig:
    aot: int
    sample_rate: int
    channel_config: int
    frame_count: int = 0
    total_samples: int = 0
    has_crc: bool = False

    @property
    def aot_name(self) -> str:
        return AOT_NAMES.get(self.aot, f"AOT {self.aot}")

    @property
    def channels(self) -> int:
        return CHANNEL_CONFIG_COUNT.get(self.channel_config, self.channel_config)

    @property
    def codec_string(self) -> str:
        return f"mp4a.40.{self.aot}"

    @property
    def duration_s(self) -> float:
        return self.total_samples / self.sample_rate if self.sample_rate else 0.0

    def config_key(self) -> tuple[int, int, int]:
        return (self.aot, self.sample_rate, self.channel_config)

    def as_dict(self) -> dict[str, Any]:
        return {
            "aot": self.aot,
            "aot_name": self.aot_name,
            "sample_rate": self.sample_rate,
            "channel_config": self.channel_config,
            "channels": self.channels,
            "codec_string": self.codec_string,
            "frames": self.frame_count,
            "duration_s": self.duration_s,
        }


def parse_adts(data: bytes, *, max_frames: int = 4096) -> AacConfig | None:
    """Read the ADTS configuration and count frames in a raw AAC payload."""
    index = 0
    length = len(data)
    config: AacConfig | None = None
    frames = 0
    while index + 7 <= length and frames < max_frames:
        if not (data[index] == 0xFF and (data[index + 1] & 0xF0) == 0xF0):
            index += 1
            continue
        protection_absent = data[index + 1] & 0x01
        aot = ((data[index + 2] >> 6) & 0x03) + 1
        sr_index = (data[index + 2] >> 2) & 0x0F
        channel_config = ((data[index + 2] & 0x01) << 2) | ((data[index + 3] >> 6) & 0x03)
        frame_length = (
            ((data[index + 3] & 0x03) << 11)
            | (data[index + 4] << 3)
            | ((data[index + 5] >> 5) & 0x07)
        )
        if frame_length < 7 or index + frame_length > length:
            break
        if sr_index >= len(SAMPLE_RATES):
            break
        if config is None:
            config = AacConfig(
                aot=aot,
                sample_rate=SAMPLE_RATES[sr_index],
                channel_config=channel_config,
                has_crc=protection_absent == 0,
            )
        frames += 1
        config.total_samples += 1024
        index += frame_length

    if config is None:
        return None
    config.frame_count = frames
    return config


def find_id3_timestamp(data: bytes) -> int | None:
    """Return the transport-stream timestamp from the leading ID3 PRIV frame, if present."""
    if len(data) < 10 or data[:3] != b"ID3":
        return None
    size = (
        (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F)
    )
    tag = data[10 : 10 + size]
    position = tag.find(ID3_PRIV_OWNER)
    if position < 0:
        return None
    payload_start = position + len(ID3_PRIV_OWNER) + 1
    payload = tag[payload_start : payload_start + 8]
    if len(payload) < 8:
        return None
    return int.from_bytes(payload, "big") & ((1 << 33) - 1)


def has_id3_timestamp(data: bytes) -> bool:
    return find_id3_timestamp(data) is not None


AC3_SAMPLE_RATES = [48000, 44100, 32000]
AC3_ACMOD_CHANNELS = {0: 2, 1: 1, 2: 2, 3: 3, 4: 3, 5: 4, 6: 4, 7: 5}


@dataclass(slots=True)
class Ac3Config:
    """AC-3 / E-AC-3 sync-frame parameters."""

    codec: str
    sample_rate: int
    channels: int
    bitrate_kbps: int | None
    frame_size: int
    lfe: bool

    def config_key(self) -> tuple[str, int, int]:
        return (self.codec, self.sample_rate, self.channels)

    def as_dict(self) -> dict[str, Any]:
        return {
            "codec": self.codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels + (1 if self.lfe else 0),
            "bitrate_kbps": self.bitrate_kbps,
            "frame_size": self.frame_size,
            "lfe": self.lfe,
        }


AC3_BITRATES = [
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    160,
    192,
    224,
    256,
    320,
    384,
    448,
    512,
    576,
    640,
]


def parse_ac3(data: bytes) -> Ac3Config | None:
    """Read the first AC-3 / E-AC-3 sync frame."""
    position = data.find(b"\x0b\x77")
    if position < 0 or position + 8 > len(data):
        return None
    body = data[position:]
    bsid = (body[5] >> 3) & 0x1F
    if bsid <= 10:  # AC-3
        fscod = (body[4] >> 6) & 0x03
        frmsizecod = body[4] & 0x3F
        if fscod >= 3 or frmsizecod // 2 >= len(AC3_BITRATES):
            return None
        acmod = (body[6] >> 5) & 0x07
        lfe_shift = {0: 4, 1: 7, 2: 4, 3: 3, 4: 3, 5: 2, 6: 2, 7: 1}[acmod]
        lfe = bool((body[6] >> (7 - (3 + lfe_shift - 1))) & 0x01)
        return Ac3Config(
            codec="ac-3",
            sample_rate=AC3_SAMPLE_RATES[fscod],
            channels=AC3_ACMOD_CHANNELS[acmod],
            bitrate_kbps=AC3_BITRATES[frmsizecod // 2],
            frame_size=0,
            lfe=lfe,
        )
    # E-AC-3
    frmsiz = (((body[2] & 0x07) << 8) | body[3]) + 1
    fscod = (body[4] >> 6) & 0x03
    acmod = (body[4] >> 1) & 0x07
    lfe = bool(body[4] & 0x01)
    sample_rate = AC3_SAMPLE_RATES[fscod] if fscod < 3 else 48000
    return Ac3Config(
        codec="ec-3",
        sample_rate=sample_rate,
        channels=AC3_ACMOD_CHANNELS.get(acmod, 2),
        bitrate_kbps=None,
        frame_size=frmsiz * 2,
        lfe=lfe,
    )
