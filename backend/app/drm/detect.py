"""What a stream is protected with, and which key it asks for.

Two things have to be known before a key can be requested: which DRM system the playlist
declares, and the key identifier the content was encrypted under.

The system comes from the playlist. `#EXT-X-KEY` and `#EXT-X-SESSION-KEY` carry a `KEYFORMAT`
that names it — the Widevine, PlayReady and FairPlay identifiers are fixed strings, and
`METHOD=AES-128` with no key format is plain HLS AES, which is not DRM at all and needs no
key server.

The key identifier comes from the media. A `KEYID` attribute carries it when the packager
writes one, and otherwise it is in the `pssh` box of the fMP4 initialisation segment — either
as a version 1 KID list or inside the Widevine protection-system protobuf. Both are read here,
the attribute first because it costs nothing.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# The registered system identifier for each DRM system, as a playlist KEYFORMAT states it
# and as a `pssh` box carries it.
WIDEVINE_SYSTEM_ID = bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed")
WIDEVINE_UUID = "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
PLAYREADY_UUID = "9a04f079-9840-4286-ab92-e65be0885f95"
FAIRPLAY_KEYFORMAT = "com.apple.streamingkeydelivery"

# Boxes worth descending into when hunting for a `pssh`. An init segment nests it under
# `moov`; the rest are where a packager occasionally puts one.
_CONTAINERS = (b"moov", b"trak", b"mdia", b"minf", b"udta")


class KeySystem(str, Enum):
    """Which protection system a playlist declares, if any."""

    NONE = "none"
    # `METHOD=AES-128` or `SAMPLE-AES` with no KEYFORMAT: the key is fetched from the URI in
    # the tag itself, so there is no licence server and no key request.
    HLS_AES = "hls_aes"
    WIDEVINE = "widevine"
    PLAYREADY = "playready"
    FAIRPLAY = "fairplay"
    # A KEYFORMAT the analyzer does not recognise. Named so a report says which.
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            "none": "Clear",
            "hls_aes": "HLS AES-128",
            "widevine": "Widevine",
            "playready": "PlayReady",
            "fairplay": "FairPlay",
            "other": "Unrecognised key format",
        }[self.value]

    @property
    def needs_key_server(self) -> bool:
        """Whether reading the payload means asking a key server for a content key."""
        return self is KeySystem.WIDEVINE


@dataclass(slots=True)
class DrmInfo:
    """What one rendition's protection amounts to."""

    system: KeySystem = KeySystem.NONE
    # Lower-case hex, no dashes. Empty until an init segment or a KEYID yields one.
    kid: str = ""
    key_formats: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    # Set once a key has been asked for, so a report can state what happened.
    key_obtained: bool = False
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def protected(self) -> bool:
        return self.system is not KeySystem.NONE

    @property
    def analysable(self) -> bool:
        """Whether the bitstream rules can read this rendition's payload."""
        return not self.protected or self.key_obtained

    def as_dict(self) -> dict[str, Any]:
        return {
            "system": self.system.value,
            "system_label": self.system.label,
            "protected": self.protected,
            "kid": self.kid,
            "key_formats": list(self.key_formats),
            "methods": list(self.methods),
            "key_obtained": self.key_obtained,
            "analysable": self.analysable,
            "reason": self.reason,
            **self.extra,
        }


def key_system(key_format: str) -> KeySystem:
    """The system one `KEYFORMAT` value names."""
    value = (key_format or "").strip().strip('"').lower()
    if not value or value == "identity":
        return KeySystem.HLS_AES
    if value.startswith("urn:uuid:"):
        uuid = value[len("urn:uuid:") :]
        if uuid == WIDEVINE_UUID:
            return KeySystem.WIDEVINE
        if uuid == PLAYREADY_UUID:
            return KeySystem.PLAYREADY
        return KeySystem.OTHER
    if value == FAIRPLAY_KEYFORMAT:
        return KeySystem.FAIRPLAY
    return KeySystem.OTHER


def describe_keys(keys: list[dict[str, str]]) -> DrmInfo:
    """What a playlist's `EXT-X-KEY` and `EXT-X-SESSION-KEY` tags amount to.

    A ladder can carry more than one: a packager commonly declares Widevine and PlayReady
    side by side for the same content. The strongest claim wins, because a channel the
    analyzer can obtain a Widevine key for is analysable whatever else is declared beside it.
    """
    formats: list[str] = []
    methods: list[str] = []
    found: list[KeySystem] = []
    kid = ""

    for attrs in keys:
        method = (attrs.get("METHOD") or "").strip().strip('"').upper()
        if method in ("", "NONE"):
            continue
        methods.append(method)
        key_format = attrs.get("KEYFORMAT", "")
        formats.append(key_format.strip('"') or "identity")
        found.append(key_system(key_format))
        if not kid:
            kid = normalise_kid(attrs.get("KEYID", ""))

    if not found:
        return DrmInfo()

    # Widevine is what this analyzer can obtain a key for, so it is preferred when a packager
    # declares several. Otherwise the first declaration stands.
    system = KeySystem.WIDEVINE if KeySystem.WIDEVINE in found else found[0]
    return DrmInfo(
        system=system,
        kid=kid,
        key_formats=tuple(dict.fromkeys(formats)),
        methods=tuple(dict.fromkeys(methods)),
    )


def normalise_kid(raw: str) -> str:
    """A key identifier as 32 lower-case hex characters, however it was written."""
    value = (raw or "").strip().strip('"').lower()
    if value.startswith("0x"):
        value = value[2:]
    value = value.replace("-", "")
    if len(value) == 32:
        try:
            int(value, 16)
        except ValueError:
            return ""
        return value
    return ""


def as_uuid(kid: str) -> str:
    """The dashed form a CPIX document asks for."""
    value = normalise_kid(kid)
    if not value:
        return ""
    return f"{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:]}"


def _read_box(data: bytes, offset: int) -> tuple[int, bytes, bytes]:
    """One ISO base media box: where the next one starts, its type, and its payload."""
    if offset + 8 > len(data):
        return len(data), b"", b""
    size = struct.unpack(">I", data[offset : offset + 4])[0]
    box_type = data[offset + 4 : offset + 8]
    if size == 1:
        if offset + 16 > len(data):
            return len(data), b"", b""
        size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
        payload = data[offset + 16 : offset + size]
    elif size == 0:
        payload = data[offset + 8 :]
        size = len(data) - offset
    else:
        payload = data[offset + 8 : offset + size]
    return offset + max(size, 8), box_type, payload


def extract_kid(data: bytes) -> str:
    """The Widevine key identifier carried by an initialisation segment's `pssh` box.

    Version 1 of the box lists key identifiers outright. Version 0 carries the
    protection-system data as a Widevine protobuf whose field 2 is the identifier, which is
    read here by walking the wire format rather than by taking a protobuf dependency for one
    field.

    Returns an empty string when the segment carries none, which is what a clear rendition
    and a PlayReady-only one both look like.
    """
    offset = 0
    while offset + 8 <= len(data):
        nxt, box_type, payload = _read_box(data, offset)
        if not box_type:
            break

        if box_type == b"pssh":
            found = _kid_from_pssh(data[offset:nxt])
            if found:
                return found
        elif box_type in _CONTAINERS:
            found = extract_kid(payload)
            if found:
                return found

        if nxt <= offset:
            break
        offset = nxt
    return ""


def _kid_from_pssh(box: bytes) -> str:
    """One `pssh` box, if it is Widevine's."""
    if len(box) < 32 or box[12:28] != WIDEVINE_SYSTEM_ID:
        return ""
    version = box[8]

    if version == 1 and len(box) >= 48:
        count = struct.unpack(">I", box[28:32])[0]
        if count >= 1:
            return box[32:48].hex()

    data_size = struct.unpack(">I", box[28:32])[0]
    proto = box[32 : 32 + data_size]
    index = 0
    while index < len(proto):
        tag = proto[index]
        index += 1
        wire, field_number = tag & 7, tag >> 3
        if wire == 0:  # varint: skip its continuation bytes
            while index < len(proto) and proto[index] & 0x80:
                index += 1
            index += 1
        elif wire == 2:  # length-delimited; field 2 is the key identifier
            if index >= len(proto):
                break
            length = proto[index]
            index += 1
            if field_number == 2 and length == 16:
                return proto[index : index + 16].hex()
            index += length
        else:
            break
    return ""
