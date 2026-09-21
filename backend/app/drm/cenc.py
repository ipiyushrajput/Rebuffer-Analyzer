"""Common Encryption: reading the boxes that say which bytes are encrypted, and undoing it.

A CMAF fragment is mostly clear. The headers, the timing, the sample table and the NAL length
prefixes are all in the open; what CENC encrypts is the payload inside each sample, and even
then only the part after a clear header. Three boxes say how:

* `tenc`, in the initialisation segment, declares the default key identifier, the size of the
  initialisation vector, and whether the track is protected at all.
* `senc`, in each fragment, carries one initialisation vector per sample and, where subsample
  encryption is used, the run of clear and encrypted byte counts inside it.
* `mdat` holds the samples themselves, laid out end to end in the order `senc` describes.

Decryption is AES-128 in counter mode, the counter being the sample's eight-byte
initialisation vector followed by eight zero bytes, running across the encrypted ranges of
that sample only — the clear ranges are copied through and do not advance it.

This is done here rather than by shelling out to a tool because the analyzer holds the segment
in memory and hands it straight to the parsers. A subprocess per segment would put a temporary
file and a process launch inside the measurement loop of a run that fetches a segment every
few seconds on every rendition, for days.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# The scheme this module implements. `cenc` is AES-CTR, which is what `SAMPLE-AES-CTR` in an
# HLS playlist means. `cbcs` is AES-CBC with a crypt/skip pattern and is a different cipher.
CENC = "cenc"
CBCS = "cbcs"

FULL_BOX_HEADER = 4
IV_ZERO_PAD = 8


class CencError(Exception):
    """The fragment could not be read. The message is what a finding states."""


@dataclass(slots=True)
class TrackEncryption:
    """What `tenc` declares about a track."""

    is_protected: bool = False
    iv_size: int = 0
    default_kid: str = ""
    scheme: str = CENC
    # Set for `cbcs`, which this module does not decrypt; carried so a report can say why.
    crypt_byte_block: int = 0
    skip_byte_block: int = 0

    @property
    def supported(self) -> bool:
        return self.is_protected and self.scheme == CENC


@dataclass(slots=True)
class SampleEncryption:
    """One sample's initialisation vector and, where it has one, its subsample map."""

    iv: bytes = b""
    # (clear_bytes, encrypted_bytes) pairs. Empty means the whole sample is encrypted.
    subsamples: list[tuple[int, int]] = field(default_factory=list)


def _boxes(data: bytes, start: int = 0, end: int | None = None):  # type: ignore[no-untyped-def]
    """Walk one level of ISO base media boxes, yielding (type, body_start, box_end)."""
    stop = len(data) if end is None else end
    offset = start
    while offset + 8 <= stop:
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        box_type = data[offset + 4 : offset + 8]
        header = 8
        if size == 1:
            if offset + 16 > stop:
                return
            size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
            header = 16
        elif size == 0:
            size = stop - offset
        if size < header:
            return
        yield box_type, offset + header, min(offset + size, stop)
        offset += size


def _find(data: bytes, path: tuple[bytes, ...], start: int = 0, end: int | None = None):  # type: ignore[no-untyped-def]
    """The body range of the first box matching a nested path, or None."""
    head, rest = path[0], path[1:]
    for box_type, body, stop in _boxes(data, start, end):
        if box_type != head:
            continue
        if not rest:
            return body, stop
        found = _find(data, rest, body, stop)
        if found is not None:
            return found
    return None


def read_track_encryption(init_segment: bytes) -> TrackEncryption:
    """What the initialisation segment says about protection.

    A clear track, or one whose `tenc` is absent, comes back unprotected — which is what makes
    a clear rendition on a protected ladder analyse without a key.
    """
    found = _find(
        init_segment,
        (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd"),
    )
    if found is None:
        return TrackEncryption()

    # `tenc` sits under the sample entry's `sinf/schi`, and the sample entry's own header
    # length varies by codec. Locating the box by name is what every demuxer does here.
    marker = init_segment.find(b"tenc", found[0], found[1])
    if marker < 0:
        return TrackEncryption()

    body = marker + 4
    if body + 20 > len(init_segment):
        return TrackEncryption()

    version = init_segment[body]
    pattern = init_segment[body + FULL_BOX_HEADER + 1]
    is_protected = init_segment[body + FULL_BOX_HEADER + 2]
    iv_size = init_segment[body + FULL_BOX_HEADER + 3]
    default_kid = init_segment[body + FULL_BOX_HEADER + 4 : body + FULL_BOX_HEADER + 20].hex()

    scheme = CENC
    crypt_block = skip_block = 0
    if version >= 1 and pattern:
        # A non-zero pattern is `cbcs`: a run of encrypted blocks followed by a run of clear
        # ones, under CBC rather than CTR.
        crypt_block, skip_block = pattern >> 4, pattern & 0x0F
        if crypt_block:
            scheme = CBCS

    # The scheme is also declared outright by `schm`, which is the authority when present.
    schm = init_segment.find(b"schm", found[0], found[1])
    if schm >= 0 and schm + 12 <= len(init_segment):
        declared = init_segment[schm + 8 : schm + 12]
        if declared == b"cbcs":
            scheme = CBCS
        elif declared == b"cenc":
            scheme = CENC

    return TrackEncryption(
        is_protected=bool(is_protected),
        iv_size=iv_size or 8,
        default_kid=default_kid,
        scheme=scheme,
        crypt_byte_block=crypt_block,
        skip_byte_block=skip_block,
    )


def read_sample_encryption(fragment: bytes, iv_size: int = 8) -> list[SampleEncryption]:
    """Each sample's initialisation vector and subsample map, from `senc`.

    A fragment with no `senc` is one whose samples are not individually protected, and comes
    back empty rather than raising: the caller then leaves it alone.
    """
    marker = fragment.find(b"senc")
    if marker < 0:
        return []

    offset = marker + 4
    if offset + 8 > len(fragment):
        return []

    flags = int.from_bytes(fragment[offset + 1 : offset + 4], "big")
    offset += FULL_BOX_HEADER
    count = struct.unpack(">I", fragment[offset : offset + 4])[0]
    offset += 4

    has_subsamples = bool(flags & 0x2)
    samples: list[SampleEncryption] = []
    size = iv_size or 8

    for _ in range(count):
        if offset + size > len(fragment):
            raise CencError("The sample encryption box ends before the samples it counts.")
        iv = fragment[offset : offset + size]
        offset += size

        subsamples: list[tuple[int, int]] = []
        if has_subsamples:
            if offset + 2 > len(fragment):
                raise CencError("The sample encryption box ends inside a subsample count.")
            entries = struct.unpack(">H", fragment[offset : offset + 2])[0]
            offset += 2
            if offset + entries * 6 > len(fragment):
                raise CencError("The sample encryption box ends inside a subsample map.")
            for _entry in range(entries):
                clear = struct.unpack(">H", fragment[offset : offset + 2])[0]
                encrypted = struct.unpack(">I", fragment[offset + 2 : offset + 6])[0]
                subsamples.append((clear, encrypted))
                offset += 6

        samples.append(SampleEncryption(iv=iv, subsamples=subsamples))

    return samples


def _mdat_range(fragment: bytes) -> tuple[int, int]:
    found = _find(fragment, (b"mdat",))
    if found is None:
        raise CencError("The fragment carries no media data box.")
    return found


def decrypt_fragment(fragment: bytes, *, key: bytes, iv_size: int = 8) -> bytes:
    """One CMAF fragment with its encrypted sample ranges decrypted in place.

    Everything outside those ranges — the boxes, the clear subsample headers, the NAL length
    prefixes — is copied through untouched, so what comes back is the packager's own bitstream
    and the parsers downstream read exactly what a player would decode.
    """
    from Crypto.Cipher import AES

    if len(key) not in (16, 32):
        raise CencError(f"A content key is 16 or 32 bytes; this one is {len(key)}.")

    samples = read_sample_encryption(fragment, iv_size=iv_size)
    if not samples:
        return fragment

    mdat_start, mdat_end = _mdat_range(fragment)
    out = bytearray(fragment)
    position = mdat_start

    for sample in samples:
        if not sample.iv:
            continue
        # CENC counts from the sample's initialisation vector, zero-padded to the block size.
        counter = sample.iv + b"\x00" * (16 - len(sample.iv))
        cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=counter)

        if not sample.subsamples:
            # No map: the sample is encrypted whole, and its length is not in `senc`. Without
            # a sample-size table there is nothing to bound it by, so it is left alone rather
            # than guessed at.
            continue

        for clear, encrypted in sample.subsamples:
            position += clear
            if encrypted:
                stop = position + encrypted
                if stop > mdat_end:
                    raise CencError(
                        "A subsample runs past the end of the media data box, so the "
                        "fragment and its encryption map disagree."
                    )
                out[position:stop] = cipher.decrypt(bytes(out[position:stop]))
                position = stop

    return bytes(out)
