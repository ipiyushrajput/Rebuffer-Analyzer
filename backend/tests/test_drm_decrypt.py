"""Turning an encrypted segment back into one the rules can read.

The decrypt itself is ffmpeg's, and this host has no ffmpeg, so what is asserted here is
everything around it: that a missing key, an empty body and a decrypt that produced too little
are each reported rather than passed on, and that a host without ffmpeg says so instead of
reporting a protected channel as clean.
"""

from __future__ import annotations

import pytest

from app.drm import decrypt
from app.media.ffprobe import FfprobeUnavailable

KEY = "00112233445566778899aabbccddeeff"


@pytest.mark.asyncio()
async def test_a_rendition_with_no_key_is_reported_rather_than_attempted() -> None:
    result = await decrypt.decrypt_segment(b"\x00" * 100, key_hex="")

    assert result.ok is False
    assert result.data == b""
    assert "No content key" in result.reason


@pytest.mark.asyncio()
async def test_an_empty_body_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decrypt, "available", lambda: True)

    result = await decrypt.decrypt_segment(b"", key_hex=KEY)

    assert result.ok is False
    assert "empty" in result.reason


@pytest.mark.asyncio()
async def test_a_host_without_ffmpeg_says_so_rather_than_reporting_a_clean_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The caller turns this into INFO-001.

    Silently returning no data would let a DRM channel finish with no bitstream findings and
    read as one that passed every check.
    """
    monkeypatch.setattr(decrypt, "available", lambda: False)

    with pytest.raises(FfprobeUnavailable):
        await decrypt.decrypt_segment(b"\x00" * 100, key_hex=KEY)


@pytest.mark.asyncio()
async def test_a_decrypt_that_produced_far_too_little_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The signature of a wrong key: the demuxer drops what it cannot read and the output
    shrinks. Handing that to the rules would report the analyzer's own error as corruption."""
    monkeypatch.setattr(decrypt, "available", lambda: True)

    async def tiny(argv, timeout=0.0, stdin=b""):  # type: ignore[no-untyped-def]
        return 0, b"x" * 10, b""

    monkeypatch.setattr(decrypt, "_run", tiny)

    result = await decrypt.decrypt_segment(b"\x00" * 1000, key_hex=KEY)

    assert result.ok is False
    assert "below the" in result.reason


@pytest.mark.asyncio()
async def test_ffmpeg_s_own_complaint_is_carried_into_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(decrypt, "available", lambda: True)

    async def refuses(argv, timeout=0.0, stdin=b""):  # type: ignore[no-untyped-def]
        return 1, b"", b"pipe:0: Invalid data found when processing input"

    monkeypatch.setattr(decrypt, "_run", refuses)

    result = await decrypt.decrypt_segment(b"\x00" * 1000, key_hex=KEY)

    assert result.ok is False
    assert "Invalid data found" in result.reason


@pytest.mark.asyncio()
async def test_a_sound_decrypt_returns_the_plaintext_and_what_it_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(decrypt, "available", lambda: True)
    captured: dict[str, object] = {}

    async def succeeds(argv, timeout=0.0, stdin=b""):  # type: ignore[no-untyped-def]
        captured["argv"] = argv
        captured["stdin"] = stdin
        return 0, b"y" * 1000, b""

    monkeypatch.setattr(decrypt, "_run", succeeds)

    result = await decrypt.decrypt_segment(b"\x00" * 900, key_hex=KEY, init_segment=b"\x11" * 100)

    assert result.ok is True
    assert result.size == 1000
    assert result.duration_ms >= 0.0

    argv = captured["argv"]
    assert "-decryption_key" in argv and KEY in argv  # type: ignore[operator]
    assert "-c" in argv and "copy" in argv, "the bitstream is copied, never re-encoded"  # type: ignore[operator]
    assert captured["stdin"] == b"\x11" * 100 + b"\x00" * 900, (
        "the initialisation segment is prepended, or the decrypt has no moov box to work from"
    )
