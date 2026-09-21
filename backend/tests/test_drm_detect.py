"""What a stream is protected with, and which key it asks for.

Nothing can be requested from a key server until both are known, and getting either wrong is
silent: a KID read one byte off returns no key, and the channel is reported as unanalysable
when the fault is the analyzer's.

The `pssh` boxes here are built to the specification rather than captured, so the test states
what the parser is expected to read rather than only that it still reads what it read before.
"""

from __future__ import annotations

import struct

from app.drm.detect import (
    WIDEVINE_SYSTEM_ID,
    DrmInfo,
    KeySystem,
    as_uuid,
    describe_keys,
    extract_kid,
    key_system,
    normalise_kid,
)

KID = "0123456789abcdef0123456789abcdef"


def box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + box_type + payload


def pssh_v1(kid_hex: str, system: bytes = WIDEVINE_SYSTEM_ID) -> bytes:
    """A version 1 `pssh`, which lists its key identifiers outright."""
    body = bytes([1, 0, 0, 0]) + system + struct.pack(">I", 1) + bytes.fromhex(kid_hex)
    return box(b"pssh", body + struct.pack(">I", 0))


def pssh_v0(kid_hex: str, system: bytes = WIDEVINE_SYSTEM_ID) -> bytes:
    """A version 0 `pssh`, whose Widevine protobuf carries the identifier as field 2."""
    proto = bytes([0x12, 16]) + bytes.fromhex(kid_hex)  # field 2, length-delimited, 16 bytes
    body = bytes([0, 0, 0, 0]) + system + struct.pack(">I", len(proto)) + proto
    return box(b"pssh", body)


# -- which system a playlist declares ----------------------------------------


def test_each_key_format_names_its_system() -> None:
    assert key_system("urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed") is KeySystem.WIDEVINE
    assert key_system("urn:uuid:9a04f079-9840-4286-ab92-e65be0885f95") is KeySystem.PLAYREADY
    assert key_system("com.apple.streamingkeydelivery") is KeySystem.FAIRPLAY


def test_a_key_with_no_format_is_plain_hls_aes_and_needs_no_key_server() -> None:
    """`METHOD=AES-128` with no KEYFORMAT fetches its key from the tag's own URI."""
    assert key_system("") is KeySystem.HLS_AES
    assert key_system("identity") is KeySystem.HLS_AES
    assert KeySystem.HLS_AES.needs_key_server is False
    assert KeySystem.WIDEVINE.needs_key_server is True


def test_a_format_the_analyzer_does_not_know_is_named_rather_than_guessed() -> None:
    assert key_system("urn:uuid:00000000-0000-0000-0000-000000000000") is KeySystem.OTHER
    assert KeySystem.OTHER.label == "Unrecognised key format"


def test_a_clear_playlist_declares_nothing() -> None:
    assert describe_keys([]).protected is False
    assert describe_keys([{"METHOD": "NONE"}]).protected is False


def test_widevine_is_preferred_when_a_packager_declares_several() -> None:
    """
    A ladder commonly carries Widevine and PlayReady side by side for the same content.

    Widevine is the one a key can be obtained for, so it decides what the channel is treated
    as; reporting PlayReady because its tag came first would call an analysable channel
    unanalysable.
    """
    info = describe_keys(
        [
            {"METHOD": "SAMPLE-AES", "KEYFORMAT": "urn:uuid:9a04f079-9840-4286-ab92-e65be0885f95"},
            {"METHOD": "SAMPLE-AES", "KEYFORMAT": "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"},
        ]
    )

    assert info.system is KeySystem.WIDEVINE
    assert len(info.key_formats) == 2, "both are still reported"


def test_a_key_id_on_the_tag_is_taken_without_reading_a_segment() -> None:
    info = describe_keys(
        [
            {
                "METHOD": "SAMPLE-AES",
                "KEYFORMAT": "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",
                "KEYID": f"0x{KID.upper()}",
            }
        ]
    )

    assert info.kid == KID, "however the packager wrote it"


# -- the key identifier in an initialisation segment -------------------------


def test_a_version_one_pssh_lists_its_key_identifier() -> None:
    assert extract_kid(pssh_v1(KID)) == KID


def test_a_version_zero_pssh_carries_it_in_the_widevine_protobuf() -> None:
    assert extract_kid(pssh_v0(KID)) == KID


def test_a_pssh_nested_under_moov_is_found() -> None:
    """Where an initialisation segment actually puts it."""
    init = box(b"ftyp", b"iso6" + b"\x00" * 8) + box(
        b"moov", box(b"mvhd", b"\x00" * 24) + pssh_v1(KID)
    )

    assert extract_kid(init) == KID


def test_another_system_s_pssh_yields_nothing() -> None:
    """A PlayReady-only channel has no Widevine key to ask for, and says so by being empty."""
    playready = bytes.fromhex("9a04f07998404286ab92e65be0885f95")

    assert extract_kid(pssh_v1(KID, system=playready)) == ""


def test_a_clear_initialisation_segment_yields_nothing() -> None:
    init = box(b"ftyp", b"iso6" + b"\x00" * 8) + box(b"moov", box(b"mvhd", b"\x00" * 24))

    assert extract_kid(init) == ""


def test_a_truncated_segment_does_not_hang_or_raise() -> None:
    """A segment cut short by a failed fetch reaches the parser like any other."""
    assert extract_kid(pssh_v1(KID)[:20]) == ""
    assert extract_kid(b"") == ""
    assert extract_kid(b"\x00\x00\x00\x00") == ""


def test_a_box_that_claims_zero_length_does_not_loop() -> None:
    """A malformed size would otherwise walk the same offset forever."""
    malformed = struct.pack(">I", 0) + b"moov" + b"\x00" * 16

    assert extract_kid(malformed) == ""


# -- how a key identifier is written -----------------------------------------


def test_a_key_identifier_is_read_in_every_spelling_a_packager_uses() -> None:
    dashed = "01234567-89ab-cdef-0123-456789abcdef"

    assert normalise_kid(dashed) == KID
    assert normalise_kid(KID.upper()) == KID
    assert normalise_kid(f'"{dashed}"') == KID
    assert normalise_kid(f"0x{KID}") == KID


def test_something_that_is_not_a_key_identifier_is_refused() -> None:
    assert normalise_kid("") == ""
    assert normalise_kid("not-a-kid") == ""
    assert normalise_kid("0123456789abcdef") == "", "sixteen characters is half of one"
    assert normalise_kid("zzzz4567-89ab-cdef-0123-456789abcdef") == ""


def test_the_cpix_document_asks_for_the_dashed_form() -> None:
    assert as_uuid(KID) == "01234567-89ab-cdef-0123-456789abcdef"
    assert as_uuid("") == ""


# -- what a report states ----------------------------------------------------


def test_a_protected_rendition_is_unanalysable_until_its_key_is_obtained() -> None:
    info = DrmInfo(system=KeySystem.WIDEVINE, kid=KID)
    assert info.protected is True
    assert info.analysable is False, "the bitstream rules have nothing to read"

    info.key_obtained = True
    assert info.analysable is True


def test_a_clear_rendition_is_analysable_without_a_key() -> None:
    assert DrmInfo().analysable is True


def test_the_payload_a_report_reads_names_the_system_in_words() -> None:
    payload = DrmInfo(
        system=KeySystem.WIDEVINE, kid=KID, reason="No key server configured."
    ).as_dict()

    assert payload["system_label"] == "Widevine"
    assert payload["protected"] is True
    assert payload["analysable"] is False
    assert payload["reason"] == "No key server configured."
