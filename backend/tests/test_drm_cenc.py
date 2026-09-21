"""Reading a CENC-protected fragment, and undoing the encryption.

A key that is right and a box walk that is wrong produce the same thing as a key that is
wrong: noise handed to the parsers, reported as corruption in someone else's stream. So the
fragments here are built to ISO/IEC 23001-7 rather than captured, the plaintext is known, and
what is asserted is that exactly the encrypted ranges came back and nothing else moved.

Nothing in this file is real content and nothing here is a real key.
"""

from __future__ import annotations

import struct

import pytest
from Crypto.Cipher import AES

from app.drm import decrypt
from app.drm.cenc import (
    CBCS,
    CENC,
    CencError,
    decrypt_fragment,
    read_sample_encryption,
    read_track_encryption,
)

KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
KID = "bb3816225016413ca5de7b6dc82c92a9"


def box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + box_type + payload


def tenc(*, kid_hex: str = KID, iv_size: int = 8, pattern: int = 0, protected: int = 1) -> bytes:
    """A track encryption box. A non-zero pattern is what makes a track `cbcs`."""
    version = 1 if pattern else 0
    body = bytes([version, 0, 0, 0, 0, pattern, protected, iv_size]) + bytes.fromhex(kid_hex)
    return box(b"tenc", body)


def init_segment(*, scheme: bytes = b"cenc", entry: bytes | None = None) -> bytes:
    """An initialisation segment whose single track is protected."""
    sinf = box(
        b"sinf",
        box(b"frma", b"avc1")
        + box(b"schm", bytes(4) + scheme + struct.pack(">I", 0x10000))
        + box(b"schi", tenc(pattern=0x19 if scheme == b"cbcs" else 0)),
    )
    sample_entry = entry if entry is not None else box(b"encv", bytes(78) + sinf)
    stsd = box(b"stsd", bytes(4) + struct.pack(">I", 1) + sample_entry)
    return box(b"ftyp", b"iso6" + bytes(8)) + box(
        b"moov",
        box(b"trak", box(b"mdia", box(b"minf", box(b"stbl", stsd)))),
    )


def senc(samples: list[tuple[bytes, list[tuple[int, int]]]]) -> bytes:
    """A sample encryption box: one initialisation vector and subsample map per sample."""
    body = bytes([0, 0, 0, 0x02]) + struct.pack(">I", len(samples))
    for iv, subsamples in samples:
        body += iv + struct.pack(">H", len(subsamples))
        for clear, encrypted in subsamples:
            body += struct.pack(">H", clear) + struct.pack(">I", encrypted)
    return box(b"senc", body)


def counter_for(iv: bytes) -> AES.CbcMode:
    """The cipher CENC specifies: the sample's IV, zero-padded to the block size."""
    return AES.new(KEY, AES.MODE_CTR, nonce=b"", initial_value=iv + bytes(16 - len(iv)))


def fragment(samples: list[tuple[bytes, list[tuple[int, int]]]], media: bytes) -> bytes:
    return box(b"moof", box(b"traf", senc(samples))) + box(b"mdat", media)


# -- what the initialisation segment declares --------------------------------


def test_the_track_encryption_box_is_read_out_of_a_protected_sample_entry() -> None:
    track = read_track_encryption(init_segment())

    assert track.is_protected is True
    assert track.iv_size == 8
    assert track.default_kid == KID
    assert track.scheme == CENC
    assert track.supported is True


def test_a_clear_initialisation_segment_comes_back_unprotected() -> None:
    """A packager commonly leaves one rendition of a protected ladder in the open."""
    clear = init_segment(entry=box(b"avc1", bytes(78)))
    track = read_track_encryption(clear)

    assert track.is_protected is False
    assert track.supported is False, "and so it is never handed to the decrypt"


def test_a_cbcs_track_is_named_rather_than_decrypted_as_though_it_were_cenc() -> None:
    """
    `cbcs` is AES-CBC over a crypt/skip pattern — a different cipher, not a variant.

    Running the CTR path over it would produce noise the bitstream rules would report as
    corruption in the packager's stream, so it is identified and refused.
    """
    track = read_track_encryption(init_segment(scheme=b"cbcs"))

    assert track.is_protected is True
    assert track.scheme == CBCS
    assert (track.crypt_byte_block, track.skip_byte_block) == (1, 9)
    assert track.supported is False


def test_a_segment_with_no_track_encryption_box_yields_nothing() -> None:
    assert read_track_encryption(b"").is_protected is False
    assert read_track_encryption(b"\x00\x00\x00\x08ftyp").is_protected is False


# -- what the fragment declares ----------------------------------------------


def test_every_sample_s_initialisation_vector_and_subsample_map_is_read() -> None:
    samples = [
        (b"\x00\x00\x00\x00\x00\x82\x8b\x8e", [(112, 1024), (5, 2048)]),
        (b"\x00\x00\x00\x00\x00\x82\x8b\x8f", [(40, 512)]),
    ]
    read = read_sample_encryption(fragment(samples, bytes(4096)))

    assert [s.iv for s in read] == [iv for iv, _ in samples]
    assert [s.subsamples for s in read] == [subsamples for _, subsamples in samples]


def test_a_fragment_with_no_sample_encryption_box_reads_as_nothing_to_do() -> None:
    assert read_sample_encryption(box(b"mdat", bytes(64))) == []


def test_a_sample_encryption_box_that_ends_early_is_refused() -> None:
    """A body cut short by a failed fetch reaches the parser like any other."""
    whole = fragment([(bytes(8), [(0, 16)]), (bytes(8), [(0, 16)])], bytes(64))
    truncated = whole[: whole.find(b"senc") + 20]

    with pytest.raises(CencError):
        read_sample_encryption(truncated)


# -- the decrypt itself ------------------------------------------------------


def test_the_encrypted_ranges_come_back_and_nothing_else_moves() -> None:
    """The property the whole thing rests on.

    A NAL length prefix or a sample header rewritten by mistake is a segment the parsers read
    as corrupt, and the channel is reported for a defect the analyzer introduced.
    """
    clear_one, plain_one = b"CLEARHEAD", b"encrypted video payload, sample one" * 3
    clear_two, plain_two = b"HEADTWO", b"and sample two, a different length" * 2

    iv_one = b"\x00\x00\x00\x00\x00\x00\x00\x01"
    iv_two = b"\x00\x00\x00\x00\x00\x00\x00\x02"
    media = (
        clear_one
        + counter_for(iv_one).encrypt(plain_one)
        + clear_two
        + counter_for(iv_two).encrypt(plain_two)
    )
    samples = [
        (iv_one, [(len(clear_one), len(plain_one))]),
        (iv_two, [(len(clear_two), len(plain_two))]),
    ]

    out = decrypt_fragment(fragment(samples, media), key=KEY, iv_size=8)

    assert len(out) == len(fragment(samples, media)), "decryption is length-preserving"
    body = out[out.find(b"mdat") + 4 :]
    assert body == clear_one + plain_one + clear_two + plain_two


def test_a_sample_split_across_several_subsamples_decrypts_as_one_run() -> None:
    """The counter runs across a sample's encrypted ranges; the clear runs do not advance it.

    Treating each subsample as a fresh counter is the mistake that decrypts the first range of
    every sample correctly and turns the rest to noise, which reads as a stream that degrades
    part-way through every segment.
    """
    iv = b"\x00\x00\x00\x00\x00\x00\x00\x07"
    first, second = b"A" * 48, b"B" * 96
    stream = counter_for(iv).encrypt(first + second)
    media = b"hdr1" + stream[: len(first)] + b"hdr2" + stream[len(first) :]
    samples = [(iv, [(4, len(first)), (4, len(second))])]

    out = decrypt_fragment(fragment(samples, media), key=KEY, iv_size=8)

    body = out[out.find(b"mdat") + 4 :]
    assert body == b"hdr1" + first + b"hdr2" + second


def test_a_fragment_with_nothing_encrypted_comes_back_byte_for_byte() -> None:
    untouched = box(b"moof", box(b"traf", b"")) + box(b"mdat", b"clear media")

    assert decrypt_fragment(untouched, key=KEY) == untouched


def test_a_subsample_running_past_the_media_data_box_is_refused() -> None:
    """The map and the fragment disagree, so what would come back is not the bitstream."""
    samples = [(b"\x00" * 8, [(0, 4096)])]

    with pytest.raises(CencError):
        decrypt_fragment(fragment(samples, bytes(64)), key=KEY, iv_size=8)


def test_a_key_that_is_not_a_key_length_is_refused() -> None:
    samples = [(b"\x00" * 8, [(0, 16)])]

    with pytest.raises(CencError):
        decrypt_fragment(fragment(samples, bytes(64)), key=b"\x00" * 7, iv_size=8)


# -- what the caller is told -------------------------------------------------


def test_a_rendition_with_no_key_is_reported_rather_than_attempted() -> None:
    result = decrypt.decrypt_segment(bytes(100), key_hex="")

    assert result.ok is False
    assert result.data == b""
    assert "No content key" in result.reason


def test_an_empty_body_is_reported() -> None:
    result = decrypt.decrypt_segment(b"", key_hex=KEY.hex())

    assert result.ok is False
    assert "empty" in result.reason


def test_a_cbcs_track_is_refused_with_the_scheme_named() -> None:
    """The caller turns this into INFO-001, so the reason has to say which scheme."""
    track = read_track_encryption(init_segment(scheme=b"cbcs"))

    result = decrypt.decrypt_segment(bytes(100), key_hex=KEY.hex(), track=track)

    assert result.ok is False
    assert "cbcs" in result.reason


def test_a_key_that_is_not_hex_is_reported_rather_than_raised() -> None:
    """A failed decrypt must not take the run's transport measurements down with it."""
    result = decrypt.decrypt_segment(bytes(100), key_hex="not-a-key")

    assert result.ok is False
    assert "hex" in result.reason


def test_a_sound_decrypt_returns_the_plaintext_and_what_it_cost() -> None:
    iv = b"\x00\x00\x00\x00\x00\x00\x00\x09"
    plain = b"sample payload" * 8
    media = counter_for(iv).encrypt(plain)
    encrypted = fragment([(iv, [(0, len(plain))])], media)

    result = decrypt.decrypt_segment(
        encrypted, key_hex=KEY.hex(), track=read_track_encryption(init_segment())
    )

    assert result.ok is True
    assert result.size == len(encrypted)
    assert result.duration_ms >= 0.0
    assert result.data[result.data.find(b"mdat") + 4 :] == plain
