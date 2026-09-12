"""Fault-injecting HLS origin for tests.

Serves a synthetic channel and, on demand, breaks it in exactly one way: a redirect chain
(including a 307 whose children are relative), a required query token, a 404 on a segment
still listed in the playlist, a delayed response, a playlist that stops advancing, or a
wrong `Content-Type`. Every rule test drives one of these switches.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


@dataclass
class Route:
    body: bytes | Callable[[dict[str, list[str]]], bytes]
    content_type: str = "application/vnd.apple.mpegurl"
    status: int = 200
    delay_s: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)
    require_query: str | None = None
    redirect_to: str | None = None
    redirect_status: int = 302


class FixtureServer:
    """A threaded HTTP origin whose routes can be rewritten between requests."""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.request_log: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.cors_enabled = True

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> str:
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> FixtureServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        assert self._server is not None, "server is not started"
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    # -- route management ------------------------------------------------------
    def add(self, path: str, route: Route) -> None:
        with self._lock:
            self.routes["/" + path.lstrip("/")] = route

    def add_text(self, path: str, text: str, **kwargs: Any) -> None:
        self.add(path, Route(body=text.encode("utf-8"), **kwargs))

    def add_bytes(
        self, path: str, data: bytes, *, content_type: str = "video/mp2t", **kwargs: Any
    ) -> None:
        self.add(path, Route(body=data, content_type=content_type, **kwargs))

    def remove(self, path: str) -> None:
        with self._lock:
            self.routes.pop("/" + path.lstrip("/"), None)

    def get(self, path: str) -> Route | None:
        with self._lock:
            return self.routes.get("/" + path.lstrip("/"))

    def hits(self, path: str) -> int:
        target = "/" + path.lstrip("/")
        return sum(1 for entry in self.request_log if entry["path"] == target)

    def clear_log(self) -> None:
        self.request_log.clear()


def _make_handler(fixture: FixtureServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            query = parse_qs(parts.query, keep_blank_values=True)
            fixture.request_log.append(
                {
                    "path": parts.path,
                    "query": parts.query,
                    "headers": dict(self.headers),
                    "ts": time.time(),
                }
            )
            route = fixture.get(parts.path)
            if route is None:
                self._respond(404, b"not found", "text/plain")
                return

            if route.redirect_to:
                self.send_response(route.redirect_status)
                self.send_header("Location", route.redirect_to)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if route.require_query and route.require_query not in query:
                self._respond(403, b"missing required token", "text/plain", extra=route.headers)
                return

            if route.delay_s:
                time.sleep(route.delay_s)

            body = route.body(query) if callable(route.body) else route.body
            self._respond(route.status, body, route.content_type, extra=route.headers)

        def _respond(
            self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if fixture.cors_enabled:
                self.send_header("Access-Control-Allow-Origin", "*")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

    return Handler


def build_simple_channel(
    server: FixtureServer,
    *,
    variant_count: int = 3,
    segment_count: int = 6,
    segment_duration: float = 6.0,
) -> str:
    """Publish a clean multi-rung channel and return its master playlist URL."""
    from tests.fixtures.synth import (
        PlaylistSpec,
        SegmentSpec,
        VariantSpec,
        build_ts_segment,
        render_master_playlist,
        render_media_playlist,
    )

    # Bitrates step by less than max_adjacent_rung_ratio so a clean channel stays clean.
    rungs = [
        ("low", 600_000, "640x360", 640, 360),
        ("mid", 1_100_000, "1280x720", 1280, 720),
        ("high", 2_000_000, "1920x1080", 1920, 1080),
    ][:variant_count]

    variants = [
        VariantSpec(name=name, bandwidth=bw, resolution=res, uri=f"{name}.m3u8")
        for name, bw, res, _w, _h in rungs
    ]
    server.add_text("master.m3u8", render_master_playlist(variants))

    for name, _bw, _res, width, height in rungs:
        spec = PlaylistSpec(
            segment_count=segment_count,
            segment_duration=segment_duration,
            target_duration=int(segment_duration),
            segment_prefix=f"{name}-seg",
        )
        server.add_text(f"{name}.m3u8", render_media_playlist(spec))
        for index in range(segment_count):
            data = build_ts_segment(
                SegmentSpec(
                    duration=segment_duration,
                    pts_offset_s=index * segment_duration,
                    width=width,
                    height=height,
                )
            )
            server.add_bytes(f"{name}-seg{index}.ts", data)

    return server.url("master.m3u8")
