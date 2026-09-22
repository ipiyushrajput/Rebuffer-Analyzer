"""Playback proxy.

A browser cannot disable TLS verification, and CDN URLs frequently carry no CORS headers,
so the player loads the stream through this endpoint. The backend fetches with
`verify=False`, follows redirects, rewrites every URI inside a playlist to route back
through the proxy — resolved against the **final** URL, with query parameters preserved —
and adds CORS headers.

Player metrics gathered this way are measured from the analyzer host and reflect the
analyzer's network path, not a TV's. The UI labels them accordingly.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from app.config import default_ua_profile, get_settings, get_thresholds
from app.hls.playlist import is_master
from app.hls.uri import resolve
from app.net.fetcher import Fetcher
from app.ws.hub import hub

logger = logging.getLogger(__name__)
router = APIRouter(tags=["proxy"])

PROXY_PATH = "/api/proxy"
PLAYLIST_TYPES = ("mpegurl", "m3u8", "vnd.apple")

# Tags whose URI attribute must be rewritten alongside the bare URI lines.
URI_ATTRIBUTE_TAGS = (
    "#EXT-X-MEDIA:",
    "#EXT-X-I-FRAME-STREAM-INF:",
    "#EXT-X-KEY:",
    "#EXT-X-MAP:",
    "#EXT-X-SESSION-KEY:",
    "#EXT-X-PART:",
    "#EXT-X-PRELOAD-HINT:",
    "#EXT-X-RENDITION-REPORT:",
)

_URI_ATTR = re.compile(r'(URI=")([^"]+)(")')

_fetcher: Fetcher | None = None
# The profile the cached client was built with. The proxy is what carries the preview's
# requests, so it has to identify as whatever Settings currently says; a client pinned for
# the life of the process would go on sending the profile that was default at startup.
_fetcher_profile = ""


def _client() -> Fetcher:
    global _fetcher, _fetcher_profile
    wanted = default_ua_profile()
    if _fetcher is not None and _fetcher_profile != wanted:
        # Replaced rather than reconfigured: the User-Agent is fixed when the client is
        # built. The old one is left to be garbage-collected with its connection pool; a
        # Settings change is rare and a live preview keeps whatever it already opened.
        _fetcher = None
    if _fetcher is None:
        settings = get_settings()
        _fetcher = Fetcher(
            ua_profile=wanted,
            # The proxy serves a live player, so it tolerates a longer stall than a check does.
            timeout_s=max(get_thresholds().request_timeout_s, 15.0),
            per_host_connections=settings.rba_per_host_connections,
        )
        _fetcher_profile = wanted
    return _fetcher


async def aclose() -> None:
    global _fetcher
    if _fetcher is not None:
        await _fetcher.aclose()
    _fetcher = None


def proxied(url: str) -> str:
    return f"{PROXY_PATH}?u={quote(url, safe='')}"


def rewrite_playlist(text: str, final_url: str) -> str:
    """Rewrite every URI in a playlist to route back through the proxy.

    Resolution is against ``final_url``, the post-redirect URL, so a redirected master's
    relative children are fetched from the host that actually holds them.
    """
    out: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            out.append(raw_line)
            continue
        if line.startswith("#"):
            if line.startswith(URI_ATTRIBUTE_TAGS):
                out.append(
                    _URI_ATTR.sub(
                        lambda m: m.group(1) + proxied(resolve(final_url, m.group(2))) + m.group(3),
                        raw_line,
                    )
                )
            else:
                out.append(raw_line)
            continue
        out.append(proxied(resolve(final_url, line)))
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


@router.get("/proxy")
async def proxy(
    request: Request,
    u: str = Query(..., description="Absolute URL to fetch, percent-encoded"),
    session: str | None = Query(default=None, description="Session to report timings to"),
) -> Response:
    if not u.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="The u parameter must be an absolute URL")

    headers: dict[str, str] = {}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    result = await _client().fetch(u, headers=headers or None)

    if session:
        await _report_timing(session, u, result)

    if result.error:
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {result.error}")

    content_type = (result.header("content-type") or "").lower()
    is_playlist = any(token in content_type for token in PLAYLIST_TYPES) or (
        result.body[:7].startswith(b"#EXTM3U")
    )

    response_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "*",
        "Cache-Control": "no-store",
        "X-RBA-Final-URL": result.final_url,
        "X-RBA-Upstream-Status": str(result.status),
    }
    for name in ("content-range", "accept-ranges"):
        value = result.header(name)
        if value:
            response_headers[name.title()] = value

    if is_playlist:
        rewritten = rewrite_playlist(result.text, result.final_url)
        response_headers["X-RBA-Playlist-Kind"] = "master" if is_master(result.text) else "media"
        return Response(
            content=rewritten,
            status_code=result.status,
            media_type="application/vnd.apple.mpegurl",
            headers=response_headers,
        )

    return Response(
        content=result.body,
        status_code=result.status,
        media_type=result.header("content-type") or "application/octet-stream",
        headers=response_headers,
    )


@router.options("/proxy")
async def proxy_preflight() -> Response:
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Max-Age": "600",
        },
    )


async def _report_timing(session_id: str, url: str, result: Any) -> None:
    """Emit the proxy's own fetch timings so a player stall lines up with a fetch event."""
    await hub.publish(
        session_id,
        "event",
        {
            "kind": "proxy_fetch",
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "data": {
                "url": url,
                "host": urlsplit(result.final_url).netloc,
                "status": result.status,
                "bytes": result.bytes_received,
                "ttfb_ms": result.timings.ttfb_ms,
                "total_ms": result.timings.total_ms,
                "measured_from": "analyzer host",
            },
        },
    )
