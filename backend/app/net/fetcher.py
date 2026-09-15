"""Outbound fetcher.

Three properties this module guarantees, because every downstream rule depends on them:

1. TLS verification is off (`verify=False`). Certificate facts are still collected, by
   `app.net.tls_inspect`, and reported as findings.
2. URLs are used verbatim — every query parameter survives. The only edit is stripping a
   trailing ``|COMPONENT=HLS`` suffix, which is a TV Plus routing marker, not part of the
   URL.
3. Redirects are followed manually, one hop at a time, so each hop's status, headers,
   timing and query-parameter survival is recorded, and child URIs resolve against the
   final post-redirect URL.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from app.config import TIZEN_USER_AGENT, USER_AGENT_PROFILES, get_thresholds

# Verification is disabled deliberately (§0.1); the warning would otherwise be emitted on
# every single fetch and bury real log lines. httpx does not use urllib3, but any library
# pulled in later that does gets the same treatment from this one call.
try:
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:  # pragma: no cover - urllib3 is optional for the async path
    pass

COMPONENT_SUFFIX = "|COMPONENT=HLS"
# The pipe arrives raw from a player configuration and percent-encoded from the channel
# catalogue. Both forms are the same routing marker.
_PIPE = r"(?:\||%7[Cc])"
COMPONENT_MARKER = re.compile(rf"{_PIPE}COMPONENT=HLS$")
COMPONENT_MARKER_PATH = re.compile(rf"{_PIPE}COMPONENT=HLS/")

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 10

CDN_HEADER_NAMES = (
    "server",
    "via",
    "x-cache",
    "x-cache-remote",
    "x-served-by",
    "x-amz-cf-pop",
    "x-amz-cf-id",
    "x-edge-location",
    "age",
    "cache-control",
    "x-akamai-request-id",
    "akamai-grn",
    "akamai-cache-status",
    "x-fastly-request-id",
    "cf-ray",
    "x-timer",
)


def strip_component_suffix(url: str) -> str:
    """Remove the TV Plus ``|COMPONENT=HLS`` routing marker. Everything else is verbatim.

    The channel catalogue serves the marker percent-encoded — ``%7CCOMPONENT=HLS`` — and the
    player configuration serves it raw, so both spellings are removed. Only the marker itself
    goes: a ``%7C`` anywhere else in the query is a pipe the origin asked for and is kept.
    """
    stripped = COMPONENT_MARKER.sub("", url)
    return COMPONENT_MARKER_PATH.sub("/", stripped)


def query_keys(url: str) -> set[str]:
    return {k for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)}


@dataclass(slots=True)
class Timings:
    """Per-request wall-clock split, in milliseconds. ``None`` means the phase was reused."""

    dns_ms: float | None = None
    connect_ms: float | None = None
    tls_ms: float | None = None
    ttfb_ms: float | None = None
    total_ms: float = 0.0
    connection_reused: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "dns_ms": self.dns_ms,
            "connect_ms": self.connect_ms,
            "tls_ms": self.tls_ms,
            "ttfb_ms": self.ttfb_ms,
            "total_ms": self.total_ms,
            "connection_reused": self.connection_reused,
        }


@dataclass(slots=True)
class Hop:
    """One request in a redirect chain."""

    url: str
    status: int
    location: str | None
    headers: dict[str, str]
    timings: Timings
    query_keys_in: set[str] = field(default_factory=set)
    query_keys_out: set[str] = field(default_factory=set)

    @property
    def dropped_query_keys(self) -> set[str]:
        return self.query_keys_in - self.query_keys_out

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "location": self.location,
            "headers": self.headers,
            "timings": self.timings.as_dict(),
            "dropped_query_keys": sorted(self.dropped_query_keys),
        }


@dataclass(slots=True)
class FetchResult:
    """Everything measured about one logical fetch, redirects included."""

    requested_url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    timings: Timings
    hops: list[Hop] = field(default_factory=list)
    error: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    http_version: str = ""
    # True when the request died on the wire — DNS, connect, TLS, timeout, reset. The origin
    # is answerable for it. `error` set with this False is the analyzer's own limit (the
    # redirect cap), which a rule reports differently.
    transport_error: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def bytes_received(self) -> int:
        return len(self.body)

    @property
    def throughput_bps(self) -> float:
        """Bits per second measured over the whole transfer."""
        if self.timings.total_ms <= 0:
            return 0.0
        return (len(self.body) * 8) / (self.timings.total_ms / 1000.0)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def redirect_count(self) -> int:
        return max(0, len(self.hops) - 1)

    @property
    def dropped_query_keys(self) -> set[str]:
        dropped: set[str] = set()
        for hop in self.hops:
            dropped |= hop.dropped_query_keys
        return dropped

    def cdn_fingerprint(self) -> dict[str, str]:
        return {k: v for k, v in self.headers.items() if k.lower() in CDN_HEADER_NAMES}

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for k, v in self.headers.items():
            if k.lower() == lowered:
                return v
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "status": self.status,
            "http_version": self.http_version,
            "bytes": self.bytes_received,
            "throughput_bps": self.throughput_bps,
            "timings": self.timings.as_dict(),
            "hops": [h.as_dict() for h in self.hops],
            "cdn": self.cdn_fingerprint(),
            "error": self.error,
            "transport_error": self.transport_error,
        }


class _TraceCollector:
    """Turns httpcore trace events into a timing split."""

    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self._connect_started: float | None = None
        self._tls_started: float | None = None
        self.timings = Timings(connection_reused=True)

    async def __call__(self, event_name: str, info: dict[str, Any]) -> None:
        now = time.perf_counter()
        if event_name == "connection.connect_tcp.started":
            self._connect_started = now
            self.timings.connection_reused = False
        elif event_name == "connection.connect_tcp.complete" and self._connect_started:
            self.timings.connect_ms = (now - self._connect_started) * 1000
        elif event_name == "connection.start_tls.started":
            self._tls_started = now
        elif event_name == "connection.start_tls.complete" and self._tls_started:
            self.timings.tls_ms = (now - self._tls_started) * 1000
        elif event_name in (
            "http11.receive_response_headers.complete",
            "http2.receive_response_headers.complete",
        ):
            self.timings.ttfb_ms = (now - self.t0) * 1000


class Fetcher:
    """Shared async client. One instance per analysis session."""

    def __init__(
        self,
        *,
        ua_profile: str = "tizen5",
        timeout_s: float | None = None,
        per_host_connections: int = 8,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.ua_profile = ua_profile
        self.user_agent = USER_AGENT_PROFILES.get(ua_profile, TIZEN_USER_AGENT)
        self.timeout_s = timeout_s if timeout_s is not None else get_thresholds().request_timeout_s
        self._extra_headers = extra_headers or {}
        limits = httpx.Limits(
            max_connections=per_host_connections * 8,
            max_keepalive_connections=per_host_connections * 4,
        )
        self._client = httpx.AsyncClient(
            verify=False,  # §0.1 — deliberate; certificates are inspected separately.
            follow_redirects=False,  # §0.2 — hops are followed manually and recorded.
            http2=True,
            limits=limits,
            timeout=httpx.Timeout(self.timeout_s),
            trust_env=False,
        )

    @property
    def default_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Accept": "*/*",
        }
        headers.update(self._extra_headers)
        return headers

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        max_redirects: int = MAX_REDIRECTS,
        timeout_s: float | None = None,
    ) -> FetchResult:
        """Fetch ``url``, following redirects manually and recording every hop."""
        requested = strip_component_suffix(url)
        current = requested
        merged = self.default_headers
        if headers:
            merged = {**merged, **headers}

        hops: list[Hop] = []
        started = time.time()
        overall_t0 = time.perf_counter()
        effective_timeout = timeout_s if timeout_s is not None else self.timeout_s

        for _hop_index in range(max_redirects + 1):
            trace = _TraceCollector()
            request = self._client.build_request(
                method,
                current,
                headers=merged,
                extensions={
                    "trace": trace,
                    "timeout": {
                        "connect": effective_timeout,
                        "read": effective_timeout,
                        "write": effective_timeout,
                        "pool": effective_timeout,
                    },
                },
            )
            hop_t0 = time.perf_counter()
            try:
                response = await self._client.send(request, stream=True)
                try:
                    body = await response.aread()
                finally:
                    await response.aclose()
            except (TimeoutError, httpx.HTTPError, OSError) as exc:
                trace.timings.total_ms = (time.perf_counter() - hop_t0) * 1000
                hops.append(
                    Hop(
                        url=current,
                        status=0,
                        location=None,
                        headers={},
                        timings=trace.timings,
                        query_keys_in=query_keys(current),
                        query_keys_out=set(),
                    )
                )
                return FetchResult(
                    requested_url=requested,
                    final_url=current,
                    status=0,
                    headers={},
                    body=b"",
                    timings=trace.timings,
                    hops=hops,
                    error=f"{type(exc).__name__}: {exc}",
                    transport_error=True,
                    started_at=started,
                    finished_at=time.time(),
                )

            trace.timings.total_ms = (time.perf_counter() - hop_t0) * 1000
            resp_headers = dict(response.headers)
            location = response.headers.get("location")
            next_url = str(response.next_request.url) if response.next_request else None
            if location and not next_url:
                next_url = str(httpx.URL(current).join(location))

            hop = Hop(
                url=current,
                status=response.status_code,
                location=location,
                headers=resp_headers,
                timings=trace.timings,
                query_keys_in=query_keys(current),
                query_keys_out=query_keys(next_url) if next_url else query_keys(current),
            )
            hops.append(hop)

            if response.status_code in REDIRECT_STATUSES and next_url:
                current = strip_component_suffix(next_url)
                continue

            total = Timings(
                dns_ms=None,
                connect_ms=trace.timings.connect_ms,
                tls_ms=trace.timings.tls_ms,
                ttfb_ms=trace.timings.ttfb_ms,
                total_ms=(time.perf_counter() - overall_t0) * 1000,
                connection_reused=trace.timings.connection_reused,
            )
            return FetchResult(
                requested_url=requested,
                final_url=current,
                status=response.status_code,
                headers=resp_headers,
                body=body,
                timings=total,
                hops=hops,
                started_at=started,
                finished_at=time.time(),
                http_version=response.http_version,
            )

        return FetchResult(
            requested_url=requested,
            final_url=current,
            status=0,
            headers={},
            body=b"",
            timings=Timings(total_ms=(time.perf_counter() - overall_t0) * 1000),
            hops=hops,
            error=f"Redirect limit of {max_redirects} exceeded",
            started_at=started,
            finished_at=time.time(),
        )

    async def fetch_range(
        self, url: str, *, start: int, length: int, timeout_s: float | None = None
    ) -> FetchResult:
        """Byte-range fetch, used for `EXT-X-BYTERANGE` segments and init-segment probes."""
        end = start + length - 1
        return await self.fetch(url, headers={"Range": f"bytes={start}-{end}"}, timeout_s=timeout_s)
