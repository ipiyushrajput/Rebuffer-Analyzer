"""Bit-level reader for H.264 / HEVC parameter sets.

A 3-byte header read is not enough to answer the questions RBA asks (`max_num_ref_frames`,
chroma format, bit depth, VUI timing), so the SPS is decoded properly: RBSP
emulation-prevention bytes are removed first, then exp-Golomb codes are read.
"""

from __future__ import annotations


def unescape_rbsp(data: bytes) -> bytes:
    """Remove emulation-prevention bytes: 0x00 0x00 0x03 -> 0x00 0x00."""
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0x00 else 0
    return bytes(out)


class BitReader:
    """MSB-first bit reader over an RBSP payload."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, *, unescape: bool = True) -> None:
        self.data = unescape_rbsp(data) if unescape else data
        self.pos = 0

    @property
    def bits_left(self) -> int:
        return len(self.data) * 8 - self.pos

    def u(self, count: int) -> int:
        """Read ``count`` bits as an unsigned integer."""
        if count <= 0:
            return 0
        if self.bits_left < count:
            raise EOFError("bitstream exhausted")
        value = 0
        for _ in range(count):
            byte = self.data[self.pos >> 3]
            bit = (byte >> (7 - (self.pos & 7))) & 1
            value = (value << 1) | bit
            self.pos += 1
        return value

    def u1(self) -> int:
        return self.u(1)

    def flag(self) -> bool:
        return self.u(1) == 1

    def ue(self) -> int:
        """Unsigned exp-Golomb."""
        leading = 0
        while True:
            if self.bits_left <= 0:
                raise EOFError("bitstream exhausted reading ue(v)")
            if self.u1() == 1:
                break
            leading += 1
            if leading > 32:
                raise ValueError("exp-Golomb prefix longer than 32 bits")
        if leading == 0:
            return 0
        return (1 << leading) - 1 + self.u(leading)

    def se(self) -> int:
        """Signed exp-Golomb."""
        value = self.ue()
        magnitude = (value + 1) // 2
        return magnitude if value % 2 == 1 else -magnitude

    def skip(self, count: int) -> None:
        self.pos = min(self.pos + count, len(self.data) * 8)

    def byte_align(self) -> None:
        self.pos = (self.pos + 7) & ~7
