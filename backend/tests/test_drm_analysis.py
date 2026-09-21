"""A protected rendition, from the initialisation segment to what the rules are handed.

What this proves is the claim the feature rests on: that a DRM channel reaches the bitstream
rules as plaintext, and that when it cannot, the run says which of the reasons applied rather
than reporting a channel nobody could read as one that passed every check.

The key server is never called. `KeyStore` is replaced with one that answers from a dict, so
what is exercised is the analyzer's own path — detection, the `tenc` read, the decrypt and the
flag the parsers see.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from Crypto.Cipher import AES

from app.analysis.collectors.segment_sampler import RungSampling, SegmentSampler
from app.drm.context import DrmContext
from app.drm.cpix import CpixCredentials
from app.drm.detect import DrmInfo, KeySystem
from app.net.fetcher import Fetcher
from tests.fixtures.server import FixtureServer, Route
from tests.test_drm_cenc import KEY, KID, box, fragment, init_segment

CONFIGURED = CpixCredentials(
    client_cert="/etc/rba/public_cert.pem",
    client_key="/etc/rba/private_key.pem",
    server_cert="/etc/rba/keyos_cert.pem",
)

IV = b"\x00\x00\x00\x00\x00\x00\x00\x11"
PLAIN = b"the payload the bitstream rules read" * 4


def encrypted_fragment() -> bytes:
    cipher = AES.new(KEY, AES.MODE_CTR, nonce=b"", initial_value=IV + bytes(8))
    return fragment([(IV, [(0, len(PLAIN))])], cipher.encrypt(PLAIN))


class StubStore:
    """A key server that answers from a dict, so no test reaches a real one."""

    def __init__(self, keys: dict[str, str] | None = None) -> None:
        self.keys = keys or {}
        self.requests_made = 0
        self.asked: list[str] = []

    async def key_for(self, kid: str, fetcher: object = None) -> str:
        self.asked.append(kid)
        self.requests_made += 1
        return self.keys.get(kid, "")

    def all_keys(self) -> dict[str, str]:
        return dict(self.keys)


def widevine_context(keys: dict[str, str] | None = None, **kwargs: object) -> DrmContext:
    context = DrmContext(
        declared=DrmInfo(system=KeySystem.WIDEVINE),
        credentials=kwargs.pop("credentials", CONFIGURED),  # type: ignore[arg-type]
    )
    context.store = StubStore(keys)  # type: ignore[assignment]
    return context


@pytest.fixture()
def served(origin: FixtureServer) -> Iterator[FixtureServer]:
    origin.add("/init.mp4", Route(body=init_segment(), content_type="video/mp4"))
    origin.add("/seg.m4s", Route(body=encrypted_fragment(), content_type="video/mp4"))
    yield origin


# -- reading the rung --------------------------------------------------------


@pytest.mark.asyncio()
async def test_the_key_identifier_comes_from_the_track_rather_than_the_ladder() -> None:
    """A ladder encrypts video and audio under different keys, so `tenc` is the authority.

    Asking for the master playlist's identifier on every rung gets one rung's key and reports
    the others as having none.
    """
    context = widevine_context({KID: KEY.hex()})
    context.declared.kid = "ffffffffffffffffffffffffffffffff"

    protection = await context.read_init("video_720", init_segment())

    assert protection.info.kid == KID
    assert protection.info.key_obtained is True
    assert protection.decryptable is True


@pytest.mark.asyncio()
async def test_a_clear_rung_on_a_protected_ladder_needs_no_key() -> None:
    """A packager commonly leaves one rendition in the open; it analyses like any clear one."""
    context = widevine_context({KID: KEY.hex()})

    protection = await context.read_init("video_360", init_segment(entry=box(b"avc1", bytes(78))))

    assert protection.info.protected is False
    assert context.store.asked == [], "and the key server is never asked for it"  # type: ignore[attr-defined]


@pytest.mark.asyncio()
async def test_a_deployment_with_no_credentials_names_the_remedy() -> None:
    context = widevine_context(credentials=CpixCredentials())

    protection = await context.read_init("video_720", init_segment())

    assert protection.decryptable is False
    assert "Settings" in protection.info.reason


@pytest.mark.asyncio()
async def test_a_key_the_server_will_not_serve_is_asked_for_once() -> None:
    """A channel whose key is refused must not ask again on every segment for days."""
    context = widevine_context({})

    await context.read_init("video_720", init_segment())
    await context.read_init("video_720", init_segment())

    assert context.store.requests_made == 2, "once per rung, not once per segment"  # type: ignore[attr-defined]
    assert context.protection("video_720").info.key_obtained is False  # type: ignore[union-attr]
    assert KID in context.protection("video_720").info.reason  # type: ignore[union-attr]


@pytest.mark.asyncio()
async def test_a_cbcs_rung_is_named_and_no_key_is_requested() -> None:
    """The CTR path over a CBC stream produces noise, which would be reported as corruption."""
    context = widevine_context({KID: KEY.hex()})

    protection = await context.read_init("video_720", init_segment(scheme=b"cbcs"))

    assert protection.decryptable is False
    assert "cbcs" in protection.info.reason
    assert context.store.asked == []  # type: ignore[attr-defined]


# -- what the sampler hands the rules ----------------------------------------


@pytest.mark.asyncio()
async def test_a_protected_segment_reaches_the_rules_as_plaintext(
    served: FixtureServer,
) -> None:
    context = widevine_context({KID: KEY.hex()})
    fetcher = Fetcher()
    sampler = SegmentSampler(
        RungSampling(variant="video_720", full=True), fetcher, encrypted=True, drm=context
    )
    try:
        await sampler.ensure_init(served.url("/init.mp4"))
        sample = await sampler.fetch_segment(
            msn=1, uri=served.url("/seg.m4s"), declared_duration=6.0
        )
    finally:
        await fetcher.aclose()

    assert sample.analysis.encrypted is False, "so every bitstream rule runs on it unchanged"
    assert sample.analysis.drm_reason == ""
    assert PLAIN in sample.decoded_body
    assert sample.decoded_body.startswith(init_segment()), (
        "the initialisation segment is in front of it, or a decoder has no moov to read"
    )
    assert context.protection("video_720").segments_decrypted == 1  # type: ignore[union-attr]


@pytest.mark.asyncio()
async def test_a_segment_with_no_key_is_left_encrypted_and_carries_the_reason(
    served: FixtureServer,
) -> None:
    """INFO-001 states this reason, so a reader knows whether the key server refused, the
    scheme is one this analyzer does not decrypt, or nothing was configured."""
    context = widevine_context({})
    fetcher = Fetcher()
    sampler = SegmentSampler(
        RungSampling(variant="video_720", full=True), fetcher, encrypted=True, drm=context
    )
    try:
        await sampler.ensure_init(served.url("/init.mp4"))
        sample = await sampler.fetch_segment(
            msn=1, uri=served.url("/seg.m4s"), declared_duration=6.0
        )
    finally:
        await fetcher.aclose()

    assert sample.analysis.encrypted is True
    assert KID in sample.analysis.drm_reason
    assert sample.decoded_body == b"", "nothing is handed to a decoder"
    assert sample.decodable_body == sample.result.body


@pytest.mark.asyncio()
async def test_a_clear_channel_is_untouched_by_any_of_this(served: FixtureServer) -> None:
    """The path a channel with no protection takes must not change at all."""
    fetcher = Fetcher()
    sampler = SegmentSampler(RungSampling(variant="video_720", full=True), fetcher)
    try:
        await sampler.ensure_init(served.url("/init.mp4"))
        sample = await sampler.fetch_segment(
            msn=1, uri=served.url("/seg.m4s"), declared_duration=6.0
        )
    finally:
        await fetcher.aclose()

    assert sample.analysis.encrypted is False
    assert sample.decoded_body == b""
    assert sample.decodable_body == sample.result.body


# -- what a report is told ---------------------------------------------------


@pytest.mark.asyncio()
async def test_the_summary_carries_no_key_material() -> None:
    """A key in a report is a key that has left the analyzer."""
    context = widevine_context({KID: KEY.hex()})
    await context.read_init("video_720", init_segment())

    rendered = repr(context.summary())

    assert KEY.hex() not in rendered
    assert CONFIGURED.client_key not in rendered
    assert KID in rendered, "the identifier is stated; the key never is"
