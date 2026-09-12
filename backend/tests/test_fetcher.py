"""Fetcher behaviour: verbatim URLs, manual redirects, and resolution against the final URL."""

from __future__ import annotations

import pytest

from app.hls import uri as uri_module
from app.net.fetcher import Fetcher, query_keys, strip_component_suffix
from tests.fixtures.server import FixtureServer, Route


def test_component_suffix_is_stripped_from_the_url() -> None:
    assert strip_component_suffix("http://h/a.m3u8|COMPONENT=HLS") == "http://h/a.m3u8"
    assert strip_component_suffix("http://h/a.m3u8?t=1") == "http://h/a.m3u8?t=1"


def test_query_parameters_are_preserved_verbatim() -> None:
    url = "http://h/a.m3u8?hdnts=exp%3D1%7Eacl%3D%2F*&ads.chan=abc&sid=9"
    assert query_keys(url) == {"hdnts", "ads.chan", "sid"}
    assert strip_component_suffix(url) == url


async def test_redirect_chain_is_recorded_hop_by_hop(origin: FixtureServer) -> None:
    origin.add("start.m3u8", Route(body=b"", redirect_to="/mid.m3u8", redirect_status=302))
    origin.add("mid.m3u8", Route(body=b"", redirect_to="/final.m3u8", redirect_status=307))
    origin.add_text("final.m3u8", "#EXTM3U\n")

    async with Fetcher() as fetcher:
        result = await fetcher.fetch(origin.url("start.m3u8"))

    assert result.ok
    assert result.redirect_count == 2
    assert [hop.status for hop in result.hops] == [302, 307, 200]
    assert result.final_url == origin.url("final.m3u8")
    assert result.text == "#EXTM3U\n"


async def test_relative_children_resolve_against_the_final_url(origin: FixtureServer) -> None:
    origin.add("a/master.m3u8", Route(body=b"", redirect_to="/b/master.m3u8", redirect_status=307))
    origin.add_text("b/master.m3u8", "#EXTM3U\n")

    async with Fetcher() as fetcher:
        result = await fetcher.fetch(origin.url("a/master.m3u8"))

    # Resolving against the requested URL would produce /a/720p.m3u8, which does not exist.
    assert uri_module.resolve(result.final_url, "720p.m3u8") == origin.url("b/720p.m3u8")


async def test_dropped_query_parameters_are_reported_per_hop(origin: FixtureServer) -> None:
    origin.add("token.m3u8", Route(body=b"", redirect_to="/plain.m3u8", redirect_status=302))
    origin.add_text("plain.m3u8", "#EXTM3U\n")

    async with Fetcher() as fetcher:
        result = await fetcher.fetch(origin.url("token.m3u8") + "?hdnts=abc&sid=1")

    assert result.dropped_query_keys == {"hdnts", "sid"}


async def test_a_url_requiring_a_token_returns_403_when_the_token_is_absent(
    origin: FixtureServer,
) -> None:
    origin.add_text("guarded.m3u8", "#EXTM3U\n", require_query="hdnts")

    async with Fetcher() as fetcher:
        without = await fetcher.fetch(origin.url("guarded.m3u8"))
        with_token = await fetcher.fetch(origin.url("guarded.m3u8") + "?hdnts=abc")

    assert without.status == 403
    assert with_token.status == 200


async def test_timings_and_throughput_are_measured(origin: FixtureServer) -> None:
    origin.add_bytes("seg0.ts", b"\x47" * 20000)

    async with Fetcher() as fetcher:
        result = await fetcher.fetch(origin.url("seg0.ts"))

    assert result.bytes_received == 20000
    assert result.timings.total_ms > 0
    assert result.timings.ttfb_ms is not None
    assert result.throughput_bps > 0


async def test_a_slow_response_beyond_the_timeout_is_an_error_not_an_exception(
    origin: FixtureServer,
) -> None:
    origin.add_text("slow.m3u8", "#EXTM3U\n", delay_s=1.5)

    async with Fetcher(timeout_s=0.4) as fetcher:
        result = await fetcher.fetch(origin.url("slow.m3u8"))

    assert result.ok is False
    assert result.error is not None
    assert result.status == 0


async def test_cdn_fingerprint_headers_are_captured(origin: FixtureServer) -> None:
    origin.add_text(
        "cdn.m3u8",
        "#EXTM3U\n",
        headers={"X-Cache": "HIT", "Age": "42", "Via": "1.1 varnish", "Server": "ATS/9"},
    )

    async with Fetcher() as fetcher:
        result = await fetcher.fetch(origin.url("cdn.m3u8"))

    fingerprint = {k.lower(): v for k, v in result.cdn_fingerprint().items()}
    assert fingerprint["x-cache"] == "HIT"
    assert fingerprint["age"] == "42"
    assert result.header("Via") == "1.1 varnish"


def test_session_tokens_are_normalised_before_two_urls_are_compared() -> None:
    a = "https://cdn/x/out/v1/1111aaaa-2222-3333-4444-555566667777/index.m3u8?aws.sessionId=A"
    b = "https://cdn/x/out/v1/9999bbbb-8888-7777-6666-555544443333/index.m3u8?aws.sessionId=B"
    assert uri_module.normalize_session_tokens(a) == uri_module.normalize_session_tokens(b)


def test_token_propagation_simulation_copies_parent_query_to_a_bare_child() -> None:
    parent = "https://cdn/master.m3u8?hdnts=exp1"
    child = "https://cdn/720p.m3u8"
    assert uri_module.propagate_query(parent, child) == "https://cdn/720p.m3u8?hdnts=exp1"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_every_redirect_status_is_followed(origin: FixtureServer, status: int) -> None:
    origin.add("r.m3u8", Route(body=b"", redirect_to="/t.m3u8", redirect_status=status))
    origin.add_text("t.m3u8", "#EXTM3U\n")

    async with Fetcher() as fetcher:
        result = await fetcher.fetch(origin.url("r.m3u8"))

    assert result.status == 200
    assert result.redirect_count == 1
