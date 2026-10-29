"""The poll log: every playlist fetch of a run, written down as it happens.

A playlist is the record of what the packager published at a moment, and the moment is what
a troubleshooting engineer needs: the window as it stood when a segment went missing, not the
window as it stands now. The in-memory history a poller keeps is bounded — a few hundred polls,
twenty minutes at a three-second cadence — so a finding from the third hour of an aging run
could only ever be shown against a playlist fetched hours later. This log is unbounded for the
life of the run and outlives the process.

Each poll is one `ManifestPoll` row: the URL asked for and the one that answered, the status,
the response headers, the redirect chain and the timing split. The body goes to
`ManifestBody`, zlib-compressed and stored once per distinct content per job; a live playlist
polled at half its target duration repeats itself on roughly every other poll, and a body
repeated thousands of times over a day is stored once.

Writes are batched and never take the run down: the log is evidence, not the measurement,
and a database that refuses a batch loses that batch — logged — while the analysis carries on.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import zlib
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.db import session as db_session
from app.db.models import ManifestBody, ManifestPoll
from app.net.fetcher import FetchResult

logger = logging.getLogger(__name__)

FLUSH_ROWS = 50
FLUSH_INTERVAL_S = 5.0


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest() if body else ""


def compress(body: bytes) -> bytes:
    return zlib.compress(body, 6)


def decompress(blob: bytes | None) -> bytes:
    return zlib.decompress(blob) if blob else b""


@dataclass(slots=True)
class PollRef:
    """How a finding names the poll it fired on."""

    seq: int
    layer: str
    variant: str
    at: dt.datetime
    msn: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "layer": self.layer,
            "variant": self.variant,
            "at": self.at.isoformat(),
            "msn": self.msn,
        }


@dataclass
class ManifestLog:
    """One job's poll log: numbers every poll, buffers rows, writes them in batches."""

    job_id: str
    enabled: bool = True
    written: int = 0
    failed_flushes: int = 0
    _seq: int = 0
    _rows: list[ManifestPoll] = field(default_factory=list)
    _bodies: dict[str, bytes] = field(default_factory=dict)
    _known: set[str] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_flush: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def resume_from(self, seq: int) -> None:
        """Carry the numbering on after a restart, so a resumed run never reuses a number."""
        self._seq = max(self._seq, seq)

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def record(
        self,
        *,
        seq: int,
        at: dt.datetime,
        layer: str,
        variant: str,
        kind: str,
        url: str,
        result: FetchResult,
        msn: int | None = None,
        last_msn: int | None = None,
    ) -> None:
        if not self.enabled:
            return
        digest = body_hash(result.body)
        row = ManifestPoll(
            job_id=self.job_id,
            seq=seq,
            ts=at,
            stream_layer=layer,
            variant=variant,
            kind=kind,
            url=url,
            final_url=result.final_url or None,
            status=int(result.status or 0),
            error=result.error,
            ttfb_ms=result.timings.ttfb_ms,
            total_ms=float(result.timings.total_ms or 0.0),
            bytes=result.bytes_received,
            msn=msn,
            last_msn=last_msn,
            headers=dict(result.headers),
            hops=[hop.as_dict() for hop in result.hops],
            timings=result.timings.as_dict(),
            body_hash=digest,
        )
        async with self._lock:
            self._rows.append(row)
            if digest and digest not in self._known and digest not in self._bodies:
                self._bodies[digest] = result.body
            due = (dt.datetime.now(dt.UTC) - self._last_flush).total_seconds() >= FLUSH_INTERVAL_S
            if len(self._rows) < FLUSH_ROWS and not due:
                return
            rows, self._rows = self._rows, []
            bodies, self._bodies = self._bodies, {}
            self._last_flush = dt.datetime.now(dt.UTC)
        await self._write(rows, bodies)

    async def flush(self) -> None:
        async with self._lock:
            rows, self._rows = self._rows, []
            bodies, self._bodies = self._bodies, {}
            self._last_flush = dt.datetime.now(dt.UTC)
        await self._write(rows, bodies)

    async def _write(self, rows: list[ManifestPoll], bodies: dict[str, bytes]) -> None:
        if not rows and not bodies:
            return
        try:
            async with db_session.session_scope() as session:
                if bodies:
                    # A resumed job has bodies stored by the process before it; those are not
                    # written twice.
                    present = set(
                        (
                            await session.execute(
                                select(ManifestBody.body_hash).where(
                                    ManifestBody.job_id == self.job_id,
                                    ManifestBody.body_hash.in_(list(bodies)),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for digest, body in bodies.items():
                        if digest in present:
                            continue
                        session.add(
                            ManifestBody(
                                job_id=self.job_id,
                                body_hash=digest,
                                size=len(body),
                                body=compress(body),
                            )
                        )
                session.add_all(rows)
            self._known.update(bodies)
            self.written += len(rows)
        except Exception as exc:
            self.failed_flushes += 1
            logger.warning(
                "%d poll(s) for job %s could not be written: %s",
                len(rows),
                self.job_id,
                db_session.describe_error(exc),
            )


async def last_seq(job_id: str) -> int:
    """The highest poll number a job has written, for a run resumed after a restart."""
    from sqlalchemy import func

    try:
        async with db_session.session_scope() as session:
            value = await session.scalar(
                select(func.max(ManifestPoll.seq)).where(ManifestPoll.job_id == job_id)
            )
        return int(value or 0)
    except Exception:
        return 0


# -- pinning and retention ---------------------------------------------------

# How far either side of an incident a poll is still evidence of it: the window that led into
# the stall and the one that came out of it.
INCIDENT_PAD_S = 30.0


def poll_seqs_of(findings: list[Any]) -> set[int]:
    """Every poll number a finding's evidence names."""
    seqs: set[int] = set()
    for finding in findings:
        evidence = getattr(finding, "evidence", None) or []
        for sample in evidence:
            poll = sample.get("poll") if isinstance(sample, dict) else None
            if isinstance(poll, dict) and isinstance(poll.get("seq"), int):
                seqs.add(poll["seq"])
    return seqs


async def pin_polls(
    job_id: str,
    *,
    seqs: set[int],
    windows: list[tuple[str | None, dt.datetime, dt.datetime]],
) -> int:
    """Mark the polls a finding fired on or an incident spanned, so the purge keeps them.

    `windows` is (variant, start, end); a window with no variant spans every rendition.
    """
    from sqlalchemy import and_, or_, update

    clauses: list[Any] = []
    if seqs:
        clauses.append(ManifestPoll.seq.in_(sorted(seqs)))
    pad = dt.timedelta(seconds=INCIDENT_PAD_S)
    for variant, start, end in windows:
        spans = and_(ManifestPoll.ts >= start - pad, ManifestPoll.ts <= end + pad)
        clauses.append(and_(spans, ManifestPoll.variant == variant) if variant else spans)
    if not clauses:
        return 0
    try:
        async with db_session.session_scope() as session:
            result = await session.execute(
                update(ManifestPoll)
                .where(ManifestPoll.job_id == job_id, or_(*clauses))
                .values(pinned=True)
            )
        return int(getattr(result, "rowcount", 0) or 0)
    except Exception as exc:
        logger.warning("polls for job %s could not be pinned: %s", job_id, exc)
        return 0


async def purge(*, cutoff: dt.datetime, pinned_cutoff: dt.datetime, running: set[str]) -> int:
    """Delete polls past retention, then every body no remaining poll refers to.

    A running job is never purged: an aging run longer than the retention window would
    otherwise lose its own first hours before it finished and pinned them.
    """
    from sqlalchemy import delete, exists, or_

    running_ids = sorted(running)
    removed = 0
    async with db_session.session_scope() as session:
        expired = or_(
            (ManifestPoll.ts < cutoff) & ManifestPoll.pinned.is_(False),
            ManifestPoll.ts < pinned_cutoff,
        )
        statement = delete(ManifestPoll).where(expired)
        if running_ids:
            statement = statement.where(ManifestPoll.job_id.not_in(running_ids))
        result = await session.execute(statement)
        removed += int(getattr(result, "rowcount", 0) or 0)

        orphan = ~exists().where(
            ManifestPoll.job_id == ManifestBody.job_id,
            ManifestPoll.body_hash == ManifestBody.body_hash,
        )
        body_statement = delete(ManifestBody).where(orphan)
        if running_ids:
            body_statement = body_statement.where(ManifestBody.job_id.not_in(running_ids))
        result = await session.execute(body_statement)
        removed += int(getattr(result, "rowcount", 0) or 0)
    return removed
