"""Playlist poller.

Every child playlist — video, audio and subtitles — is polled at `TARGETDURATION / 2`, the
cadence the A-Sequence Detector validated. Each snapshot is kept so a later poll can be
compared against it and so the UI can show the playlist exactly as it was at any moment.

A session polls a ladder through `LadderPoller`: every rendition of one layer is requested in
the same **round**, concurrently, so the snapshots a cross-variant check compares were taken
within one request latency of each other. Polling each rendition on its own clock left them
seconds apart — segment sampling runs between polls and every loop drifted on its own — and a
packager that published one rung a moment before the next read as a ladder whose media and
discontinuity sequence numbers disagreed.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.hls.playlist import MediaPlaylist, parse_media
from app.net.fetcher import Fetcher, FetchResult

MIN_POLL_INTERVAL_S = 1.0
DEFAULT_POLL_INTERVAL_S = 3.0

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Snapshot:
    """One poll of one playlist."""

    at: dt.datetime
    variant: str
    url: str
    result: FetchResult
    playlist: MediaPlaylist | None
    raw: str = ""
    # Set when the body arrived and the analyzer's own parser raised on it. That is the
    # analyzer's defect, not the stream's, so it is logged rather than charged to the origin.
    parse_error: str = ""
    # The poll's number in the job's poll log, set by the engine when it handles the poll.
    seq: int = 0
    # The ladder round this poll belongs to (`LadderPoller`); 0 for a poll made on its own.
    round: int = 0
    # When the request went out. `at` is when the response was complete.
    requested_at: dt.datetime | None = None

    @property
    def ok(self) -> bool:
        return self.result.ok and self.playlist is not None

    def summary(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at.isoformat(),
            "variant": self.variant,
            "url": self.url,
            "status": self.result.status,
            "msn": self.playlist.media_sequence if self.playlist else None,
            "last_msn": self.playlist.last_msn if self.playlist else None,
            "dsn": self.playlist.discontinuity_sequence if self.playlist else None,
            "segments": len(self.playlist.segments) if self.playlist else 0,
            "window_s": self.playlist.total_duration if self.playlist else 0.0,
            "target_duration": self.playlist.target_duration if self.playlist else None,
            "ttfb_ms": self.result.timings.ttfb_ms,
            "total_ms": self.result.timings.total_ms,
            "bytes": self.result.bytes_received,
            "headers": self.result.cdn_fingerprint(),
            "error": self.result.error,
            "parse_error": self.parse_error,
        }


SnapshotHandler = Callable[[Snapshot], Awaitable[None]]


@dataclass
class PollTarget:
    variant: str
    url: str
    kind: str = "video"  # video | audio | subtitles
    history: list[Snapshot] = field(default_factory=list)

    @property
    def latest(self) -> Snapshot | None:
        return self.history[-1] if self.history else None

    @property
    def previous(self) -> Snapshot | None:
        return self.history[-2] if len(self.history) >= 2 else None


class PlaylistPoller:
    """Polls one media playlist until stopped."""

    def __init__(
        self,
        target: PollTarget,
        fetcher: Fetcher,
        *,
        on_snapshot: SnapshotHandler,
        history_limit: int = 400,
    ) -> None:
        self.target = target
        self.fetcher = fetcher
        self.on_snapshot = on_snapshot
        self.history_limit = history_limit
        self.interval_s = DEFAULT_POLL_INTERVAL_S
        # A handler that raises is a defect in the analyzer, not in the stream. It is counted
        # and kept, so the session reports the defect instead of reporting no measurement.
        self.failures = 0
        self.last_failure = ""
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"poll:{self.target.variant}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def poll_once(self) -> Snapshot:
        requested_at = dt.datetime.now(dt.UTC)
        result = await self.fetcher.fetch(self.target.url)
        at = dt.datetime.now(dt.UTC)
        playlist: MediaPlaylist | None = None
        raw = ""
        parse_error = ""
        if result.ok:
            raw = result.text
            try:
                playlist = parse_media(raw, result.final_url)
            except Exception as exc:
                # The body arrived; the parser is what failed. Record it as the analyzer's
                # own error and keep polling — the next poll may parse.
                parse_error = f"{type(exc).__name__}: {exc}"
                log.exception(
                    "Media playlist parse failed for %s (%s bytes from %s)",
                    self.target.variant,
                    result.bytes_received,
                    result.final_url,
                )
            if playlist is not None and playlist.target_duration:
                self.interval_s = max(MIN_POLL_INTERVAL_S, playlist.target_duration / 2)

        snapshot = Snapshot(
            at=at,
            variant=self.target.variant,
            url=self.target.url,
            result=result,
            playlist=playlist,
            raw=raw,
            parse_error=parse_error,
            requested_at=requested_at,
        )
        self.target.history.append(snapshot)
        if len(self.target.history) > self.history_limit:
            del self.target.history[: len(self.target.history) - self.history_limit]
        return snapshot

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                snapshot = await self.poll_once()
                await self.on_snapshot(snapshot)
            except asyncio.CancelledError:
                raise
            except Exception:
                # The loop survives a failed poll, but the failure is never silent. This
                # handler once swallowed the traceback, which turned a crash in a detector
                # into a session that polled playlists and sampled no segments, with nothing
                # anywhere to say why.
                self.failures += 1
                self.last_failure = traceback.format_exc()
                log.exception(
                    "Playlist poll handling failed for %s (failure %d)",
                    self.target.variant,
                    self.failures,
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except TimeoutError:
                continue


RoundHandler = Callable[[int, list[tuple["PlaylistPoller", Snapshot]]], Awaitable[None]]


class LadderPoller:
    """Polls every rendition of one layer together, one round at a time.

    A round requests every playlist at once and hands the whole round to `on_round`, which
    handles each snapshot and then compares the renditions with each other. Rounds start on a
    fixed cadence — the shortest poll interval of the ladder, measured from the start of the
    previous round — so handling a round never pushes the next one later than it has to be; a
    round that takes longer than the interval is followed immediately by the next.

    Each rendition keeps its own `PlaylistPoller` for its history, its interval and its count
    of handler failures, which `AnalysisSession.sampling_state()` reports.
    """

    def __init__(self, pollers: list[PlaylistPoller], *, on_round: RoundHandler) -> None:
        self.pollers = pollers
        self.on_round = on_round
        self.rounds = 0
        self.failures = 0
        self.last_failure = ""
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def interval_s(self) -> float:
        if not self.pollers:
            return DEFAULT_POLL_INTERVAL_S
        return max(MIN_POLL_INTERVAL_S, min(p.interval_s for p in self.pollers))

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="poll:ladder")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def poll_round(self) -> list[tuple[PlaylistPoller, Snapshot]]:
        """Request every playlist of the ladder at once."""
        self.rounds += 1
        snapshots = await asyncio.gather(*(poller.poll_once() for poller in self.pollers))
        for snapshot in snapshots:
            snapshot.round = self.rounds
        return list(zip(self.pollers, snapshots, strict=True))

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            started = loop.time()
            try:
                polled = await self.poll_round()
                await self.on_round(self.rounds, polled)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A round that fails is counted and logged, never swallowed; the next round
                # runs on schedule.
                self.failures += 1
                self.last_failure = traceback.format_exc()
                log.exception(
                    "Ladder poll round %d failed (failure %d)", self.rounds, self.failures
                )
            remaining = self.interval_s - (loop.time() - started)
            if remaining <= 0:
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=remaining)
            except TimeoutError:
                continue
