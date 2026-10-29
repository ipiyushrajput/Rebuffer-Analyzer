"""Re-analyse a recorded run with the current settings.

A run writes every playlist poll to the database (`app/jobs/manifest_log.py`) and, when it was
asked to record, every segment to `RBA_RECORDINGS_DIR` (`app/analysis/recorder.py`). A replay
puts those back in front of the engine through a `Fetcher` that answers from them instead of the
network, so a threshold changed in Settings, a rule added since, or a severity reassigned is
measured against the very responses the original run received — not against a stream that has
moved on.

What a replay serves:

* **Playlists** — each URL answers with the poll the original run received at the same offset
  into the run, with its status, headers, redirect chain and timing split. Replayed at 1×, so
  freshness, cadence and incident timing keep their meaning.
* **Segments** — the recorded file, with the delivery timing the original run measured when its
  sampler fetched that segment. A segment the original sampler did not fetch has no timing: it
  is served with none, so the delivery checks on it do not run, and the replay counts it.
* **Anything else the original run did not store** — a segment outside an aging run's incident
  clips, a rendition it did not poll — is a gap: no rule reads it as a failed fetch, and the
  replay counts it.

Not re-measured: DNS, TLS and the network itself. A key request to the licence server is a
POST and goes out live, because no key is ever stored (non-negotiable 7).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.db import session as db_session
from app.db.models import ManifestBody, ManifestPoll, SegmentSample
from app.jobs.manifest_log import decompress
from app.net.fetcher import MAX_REDIRECTS, Fetcher, FetchResult, Hop, Timings

logger = logging.getLogger(__name__)

BODY_CACHE = 256


@dataclass(slots=True)
class _Poll:
    offset_s: float
    status: int
    error: str | None
    final_url: str
    headers: dict[str, str]
    hops: list[dict[str, Any]]
    timings: dict[str, Any]
    body_hash: str


@dataclass(slots=True)
class _File:
    path: Path | None
    status: int
    # The object in storage, when the recording was moved there at the end of its run.
    key: str | None = None


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


@dataclass
class ReplaySource:
    """Everything a run stored that a replay serves back, indexed by request."""

    job_id: str
    duration_s: float
    playlists: dict[str, list[_Poll]] = field(default_factory=dict)
    segments: dict[tuple[str, str | None], _File] = field(default_factory=dict)
    inits: dict[str, _File] = field(default_factory=dict)
    timings: dict[str, tuple[float | None, float]] = field(default_factory=dict)
    recorded: bool = False

    @classmethod
    async def load(cls, job_id: str, recordings_root: Path) -> ReplaySource:
        """Read the poll log, the sampler's timings and the recording index of `job_id`.

        No query here orders rows: the poll log is sorted by `seq` in memory, so the read never
        becomes a filesort over JSON headers and redirect chains (see `app/db/paging.py`).
        """
        async with db_session.session_scope() as session:
            polls = (
                await session.execute(
                    select(
                        ManifestPoll.seq,
                        ManifestPoll.ts,
                        ManifestPoll.url,
                        ManifestPoll.final_url,
                        ManifestPoll.status,
                        ManifestPoll.error,
                        ManifestPoll.headers,
                        ManifestPoll.hops,
                        ManifestPoll.timings,
                        ManifestPoll.body_hash,
                    ).where(ManifestPoll.job_id == job_id)
                )
            ).all()
            samples = (
                await session.execute(
                    select(
                        SegmentSample.uri, SegmentSample.ttfb_ms, SegmentSample.download_ms
                    ).where(SegmentSample.job_id == job_id)
                )
            ).all()

        rows = sorted(polls, key=lambda row: row.seq)
        if not rows:
            return cls(job_id=job_id, duration_s=0.0)
        start = min(_utc(row.ts) for row in rows)
        source = cls(job_id=job_id, duration_s=0.0)
        for row in rows:
            offset = max(0.0, (_utc(row.ts) - start).total_seconds())
            source.duration_s = max(source.duration_s, offset)
            source.playlists.setdefault(row.url, []).append(
                _Poll(
                    offset_s=offset,
                    status=int(row.status or 0),
                    error=row.error,
                    final_url=row.final_url or row.url,
                    headers=dict(row.headers or {}),
                    hops=list(row.hops or []),
                    timings=dict(row.timings or {}),
                    body_hash=row.body_hash or "",
                )
            )
        for sample in samples:
            if sample.uri and sample.download_ms:
                source.timings[sample.uri] = (sample.ttfb_ms, float(sample.download_ms))

        directory = recordings_root / job_id
        index = directory / "index.jsonl"
        if index.exists():
            source.recorded = True
            for line in index.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("file") and (directory / entry["file"]).exists():
                    source.segments[(entry["uri"], entry.get("byterange"))] = _File(
                        path=directory / entry["file"], status=int(entry.get("status") or 200)
                    )
                if (
                    entry.get("map_uri")
                    and entry.get("init")
                    and (directory / entry["init"]).exists()
                ):
                    source.inits[entry["map_uri"]] = _File(
                        path=directory / entry["init"], status=200
                    )
        else:
            await source._load_stored_recording(job_id)
        return source

    async def _load_stored_recording(self, job_id: str) -> None:
        """The recording's index from object storage; its files are read when asked for."""
        from app.analysis import recorder as recorder_module
        from app.storage import get_storage, keys, runs

        prefix = await runs.known_prefix(job_id)
        if not prefix:
            return
        storage = get_storage()
        for mode in (recorder_module.FULL, recorder_module.INCIDENTS):
            raw = await storage.get_bytes(keys.recording_key(prefix, mode, "index.jsonl"))
            if raw is None:
                continue
            self.recorded = True
            for line in raw.decode("utf-8", errors="replace").splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("file"):
                    self.segments[(entry["uri"], entry.get("byterange"))] = _File(
                        path=None,
                        status=int(entry.get("status") or 200),
                        key=keys.recording_key(prefix, mode, entry["file"]),
                    )
                if entry.get("map_uri") and entry.get("init"):
                    self.inits[entry["map_uri"]] = _File(
                        path=None, status=200, key=keys.recording_key(prefix, mode, entry["init"])
                    )
            return

    @property
    def available(self) -> bool:
        return bool(self.playlists)


@dataclass
class ReplayStats:
    playlists_served: int = 0
    segments_timed: int = 0
    segments_untimed: int = 0
    gaps: int = 0
    gap_examples: list[str] = field(default_factory=list)

    def statement(self, source: ReplaySource) -> str:
        parts = [
            f"This run replays run {source.job_id} at 1× from what it stored, measured against "
            f"the thresholds and rules in force now: {self.playlists_served} playlist "
            "response(s) were served from its poll log"
        ]
        if source.recorded:
            parts.append(
                f"{self.segments_timed + self.segments_untimed} segment(s) from its recording, "
                f"{self.segments_timed} of them with the delivery timing the original run "
                f"measured and {self.segments_untimed} without one, so the delivery checks did "
                "not run on those"
            )
        else:
            parts.append("no segment, because the original run did not record the stream")
        if self.gaps:
            parts.append(
                f"{self.gaps} request(s) asked for something the original run never stored and "
                "were not measured"
            )
        return (
            "; ".join(parts) + ". DNS, TLS and network delivery were not re-measured: every "
            "timing in this run is the original run's."
        )

    def as_dict(self, source: ReplaySource) -> dict[str, Any]:
        return {
            "of": source.job_id,
            "playlists_served": self.playlists_served,
            "segments_timed": self.segments_timed,
            "segments_untimed": self.segments_untimed,
            "gaps": self.gaps,
            "gap_examples": self.gap_examples,
            "recorded": source.recorded,
            "statement": self.statement(source),
        }


def _hop(data: dict[str, Any]) -> Hop:
    timings = data.get("timings") or {}
    return Hop(
        url=str(data.get("url") or ""),
        status=int(data.get("status") or 0),
        location=data.get("location"),
        headers=dict(data.get("headers") or {}),
        timings=_timings(timings),
        # The stored hop carries what was dropped, not both sides; this restores the difference.
        query_keys_in=set(data.get("dropped_query_keys") or []),
    )


def _timings(data: dict[str, Any]) -> Timings:
    return Timings(
        dns_ms=data.get("dns_ms"),
        connect_ms=data.get("connect_ms"),
        tls_ms=data.get("tls_ms"),
        ttfb_ms=data.get("ttfb_ms"),
        total_ms=float(data.get("total_ms") or 0.0),
        connection_reused=bool(data.get("connection_reused")),
    )


def _range_key(headers: dict[str, str] | None) -> str | None:
    """`Range: bytes=a-b` as the playlist spells it, `length@offset`."""
    value = next((v for k, v in (headers or {}).items() if k.lower() == "range"), None)
    if not value or not value.startswith("bytes="):
        return None
    first, _, last = value[len("bytes=") :].partition("-")
    if not first.isdigit() or not last.isdigit():
        return None
    return f"{int(last) - int(first) + 1}@{int(first)}"


class ReplayFetcher(Fetcher):
    """A `Fetcher` that answers from a recorded run instead of the network."""

    def __init__(self, source: ReplaySource, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.source = source
        self.stats = ReplayStats()
        self._t0 = time.monotonic()
        self._bodies: dict[str, bytes] = {}

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        max_redirects: int = MAX_REDIRECTS,
        timeout_s: float | None = None,
    ) -> FetchResult:
        if method != "GET" or content is not None:
            # A key exchange: keys are never stored, so it is asked for again.
            return await super().fetch(
                url,
                method=method,
                headers=headers,
                content=content,
                max_redirects=max_redirects,
                timeout_s=timeout_s,
            )
        polls = self.source.playlists.get(url)
        if polls:
            return await self._playlist(url, polls)
        byterange = _range_key(headers)
        recorded = self.source.segments.get((url, byterange))
        if recorded is None and byterange is None:
            recorded = self.source.inits.get(url)
        if recorded is not None:
            return await self._file(url, recorded)
        return self._gap(url)

    async def _playlist(self, url: str, polls: list[_Poll]) -> FetchResult:
        elapsed = time.monotonic() - self._t0
        chosen = polls[0]
        for poll in polls:
            if poll.offset_s > elapsed:
                break
            chosen = poll
        self.stats.playlists_served += 1
        now = time.monotonic()
        return FetchResult(
            requested_url=url,
            final_url=chosen.final_url,
            status=chosen.status,
            headers=chosen.headers,
            body=await self._body(chosen.body_hash),
            timings=_timings(chosen.timings),
            hops=[_hop(hop) for hop in chosen.hops],
            error=chosen.error,
            started_at=now,
            finished_at=now,
        )

    async def _body(self, digest: str) -> bytes:
        if not digest:
            return b""
        if digest in self._bodies:
            return self._bodies[digest]
        async with db_session.session_scope() as session:
            blob = await session.scalar(
                select(ManifestBody.body).where(
                    ManifestBody.job_id == self.source.job_id, ManifestBody.body_hash == digest
                )
            )
        body = decompress(blob) if blob else b""
        if len(self._bodies) >= BODY_CACHE:
            self._bodies.pop(next(iter(self._bodies)))
        self._bodies[digest] = body
        return body

    async def _file(self, url: str, recorded: _File) -> FetchResult:
        if recorded.path is not None:
            body = await asyncio.to_thread(recorded.path.read_bytes)
        else:
            from app.storage import get_storage

            stored = await get_storage().get_bytes(recorded.key or "")
            if stored is None:
                return self._gap(url)
            body = stored
        timing = self.source.timings.get(url)
        if timing is not None:
            self.stats.segments_timed += 1
            timings = Timings(ttfb_ms=timing[0], total_ms=timing[1])
        else:
            # No timing was measured for this file: none is invented. A zero total and no
            # first byte leave the delivery checks nothing to read.
            self.stats.segments_untimed += 1
            timings = Timings(total_ms=0.0)
        now = time.monotonic()
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=recorded.status,
            headers={},
            body=body,
            timings=timings,
            started_at=now,
            finished_at=now,
        )

    def _gap(self, url: str) -> FetchResult:
        self.stats.gaps += 1
        if len(self.stats.gap_examples) < 5:
            self.stats.gap_examples.append(url)
        now = time.monotonic()
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=0,
            headers={},
            body=b"",
            timings=Timings(),
            error="not stored by the replayed run",
            started_at=now,
            finished_at=now,
            replay_gap=True,
        )
