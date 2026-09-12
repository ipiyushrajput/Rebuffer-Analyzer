"""DNS inspection.

The Tizen fleet has hosts without IPv6 transit. A CDN that publishes AAAA records to those
devices makes them fail while every other client succeeds, so A and AAAA exposure and
per-family reachability are measured and reported separately.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


@dataclass(slots=True)
class DnsResult:
    host: str
    a_records: list[str] = field(default_factory=list)
    aaaa_records: list[str] = field(default_factory=list)
    cname_chain: list[str] = field(default_factory=list)
    resolve_ms: float = 0.0
    error: str | None = None
    ipv4_reachable: bool | None = None
    ipv6_reachable: bool | None = None

    @property
    def cname_depth(self) -> int:
        return len(self.cname_chain)

    def as_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "a": self.a_records,
            "aaaa": self.aaaa_records,
            "cname_chain": self.cname_chain,
            "cname_depth": self.cname_depth,
            "resolve_ms": self.resolve_ms,
            "ipv4_reachable": self.ipv4_reachable,
            "ipv6_reachable": self.ipv6_reachable,
            "error": self.error,
        }


async def _tcp_reachable(address: str, port: int, family: int, timeout: float = 3.0) -> bool:
    try:
        fut = asyncio.open_connection(host=address, port=port, family=family)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        with contextlib.suppress(TimeoutError, OSError):
            await writer.wait_closed()
        del reader
        return True
    except (TimeoutError, OSError):
        return False


async def resolve(url_or_host: str, *, probe_reachability: bool = True) -> DnsResult:
    """Resolve A and AAAA records for the host of ``url_or_host`` and probe each family."""
    if "://" in url_or_host:
        parts = urlsplit(url_or_host)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
    else:
        host = url_or_host
        port = 443

    result = DnsResult(host=host)
    if not host:
        result.error = "URL carries no hostname"
        return result

    loop = asyncio.get_running_loop()
    t0 = time.perf_counter()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        result.resolve_ms = (time.perf_counter() - t0) * 1000
        result.error = f"DNS resolution failed: {exc}"
        return result
    result.resolve_ms = (time.perf_counter() - t0) * 1000

    for family, _type, _proto, _canon, sockaddr in infos:
        addr = str(sockaddr[0])
        if family == socket.AF_INET and addr not in result.a_records:
            result.a_records.append(addr)
        elif family == socket.AF_INET6 and addr not in result.aaaa_records:
            result.aaaa_records.append(addr)

    try:
        canonical = socket.gethostbyname_ex(host)[1]
        result.cname_chain = list(canonical)
    except (OSError, socket.gaierror):
        result.cname_chain = []

    if probe_reachability:
        if result.a_records:
            result.ipv4_reachable = await _tcp_reachable(result.a_records[0], port, socket.AF_INET)
        if result.aaaa_records:
            result.ipv6_reachable = await _tcp_reachable(
                result.aaaa_records[0], port, socket.AF_INET6
            )
    return result
