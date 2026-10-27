"""A redirected segment is followed, fetched from where it landed, and analysed from there.

Redirects are followed manually so every hop is recorded (non-negotiable 2). These tests hold
that a segment redirected to a different path or a different host is the bytes the analyzer
checks, that the Range of a byte-range segment survives the hop, and that the redirect rules
report what the redirect broke rather than every redirect a CDN makes on purpose.
"""

from __future__ import annotations

import gzip

from app.analysis.engine import AnalysisSession, SessionOptions
from app.analysis.rules.base import StreamLayer
from app.analysis.rules.transport import check_http
from app.config import Thresholds
from app.net.fetcher import Fetcher
from tests.fixtures.server import FixtureServer, Route, build_simple_channel


def _other_host(origin: FixtureServer, path: str) -> str:
    """The same server under a different host name, as a CDN edge on another domain."""
    return origin.url(path).replace("127.0.0.1", "localhost")


async def test_a_segment_redirected_to_another_host_is_fetched_and_analysed(
    origin: FixtureServer,
) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=4)
    # Move segment 2's bytes to an edge path, and redirect the listed URI there.
    moved = origin.get("low-seg2.ts")
    assert moved is not None
    origin.add("edge/low-seg2.ts", moved)
    origin.add(
        "low-seg2.ts",
        Route(body=b"", redirect_to=_other_host(origin, "edge/low-seg2.ts")),
    )

    session = AnalysisSession(
        session_id="redirect",
        playback_url=url,
        options=SessionOptions(duration_s=4.0, max_segment_samples=20),
    )
    result = await session.run()

    paths = [entry["path"] for entry in origin.request_log]
    assert "/low-seg2.ts" in paths and "/edge/low-seg2.ts" in paths
    ((variant, sampler),) = session.layers[StreamLayer.PLAYBACK].samplers.items()
    assert 2 in sampler.sampling.fetched_msns
    ids = {f.rule.id for f in result.findings if f.variant == variant}
    # Fetched, parsed and judged like any other segment: no failed download, no unknown
    # container, and no host-change notice — a segment has no child URIs to resolve.
    assert not ids & {"SEG-018", "SEG-001", "SEG-006", "HTTP-005", "HTTP-003", "HTTP-002"}, ids


async def test_a_redirect_that_drops_the_token_is_reported_only_when_it_is_refused(
    origin: FixtureServer,
) -> None:
    origin.add("signed/seg.ts", Route(body=b"\x47" * 188 * 4, content_type="video/mp2t"))
    origin.add("refused/seg.ts", Route(body=b"no", status=403, content_type="text/plain"))
    origin.add("a.ts", Route(body=b"", redirect_to=origin.url("signed/seg.ts?sig=edge")))
    origin.add("b.ts", Route(body=b"", redirect_to=origin.url("refused/seg.ts")))
    fetcher = Fetcher()
    try:
        ok = await fetcher.fetch(origin.url("a.ts?token=abc"))
        refused = await fetcher.fetch(origin.url("b.ts?token=abc"))
    finally:
        await fetcher.aclose()

    t = Thresholds()
    ok_ids = {
        f.rule.id for f in check_http(ok, layer=StreamLayer.PLAYBACK, thresholds=t, kind="segment")
    }
    refused_ids = {
        f.rule.id
        for f in check_http(refused, layer=StreamLayer.PLAYBACK, thresholds=t, kind="segment")
    }
    assert ok.ok and "token" in ok.dropped_query_keys
    assert "HTTP-002" not in ok_ids
    assert {"HTTP-002", "HTTP-004"} <= refused_ids


async def test_the_range_of_a_byte_range_segment_survives_the_redirect(
    origin: FixtureServer,
) -> None:
    origin.add("edge/pack.ts", Route(body=b"\x47" * 188 * 10, content_type="video/mp2t"))
    origin.add("pack.ts", Route(body=b"", redirect_to=_other_host(origin, "edge/pack.ts")))
    fetcher = Fetcher()
    try:
        result = await fetcher.fetch_range(origin.url("pack.ts"), start=188, length=376)
    finally:
        await fetcher.aclose()

    assert result.redirect_count == 1
    edge = [e for e in origin.request_log if e["path"] == "/edge/pack.ts"]
    assert edge and edge[-1]["headers"].get("Range") == "bytes=188-563"


async def test_a_gzip_encoded_playlist_is_not_read_as_truncated(origin: FixtureServer) -> None:
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:0\n" + (
        "#EXTINF:6.0,\nseg.ts\n" * 20
    )
    origin.add(
        "zipped.m3u8",
        Route(body=gzip.compress(text.encode()), headers={"Content-Encoding": "gzip"}),
    )
    origin.add("short.m3u8", Route(body=text.encode()[:40], headers={"Content-Length": "400"}))
    fetcher = Fetcher()
    try:
        zipped = await fetcher.fetch(origin.url("zipped.m3u8"))
    finally:
        await fetcher.aclose()

    assert zipped.text == text
    assert zipped.wire_bytes < zipped.bytes_received
    ids = {
        f.rule.id for f in check_http(zipped, layer=StreamLayer.PLAYBACK, thresholds=Thresholds())
    }
    assert "HTTP-010" not in ids
