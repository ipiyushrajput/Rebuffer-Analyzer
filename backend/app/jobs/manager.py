"""Job manager.

Holds every running analysis behind a global concurrency semaphore, persists job state so
an aging or bulk job resumes after a backend restart, and streams events to the WebSocket
hub while writing samples to the database.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.analysis.engine import AnalysisResult, AnalysisSession, SessionOptions
from app.config import get_settings, get_thresholds
from app.db import session as db_session
from app.db.models import Finding as FindingRow
from app.db.models import Incident as IncidentRow
from app.db.models import Job as JobRow
from app.db.models import PlaylistSample, PlaylistSnapshot, SegmentSample, VirtualBufferSample
from app.jobs.samples import SampleRecorder

logger = logging.getLogger(__name__)

JobType = str  # realtime | aging | bulk
ResultHandler = Callable[["JobHandle", AnalysisResult], Awaitable[None]]

# A realtime session with no client attached is abandoned after this long.
REALTIME_IDLE_TIMEOUT_S = 900.0

# How long a cancel waits for the job to settle before it reports back.
CANCEL_TIMEOUT_S = 30.0


@dataclass
class JobHandle:
    """A running or finished analysis."""

    id: str
    type: JobType
    channel_name: str
    urls: dict[str, str | None]
    options: SessionOptions
    session: AnalysisSession | None = None
    task: asyncio.Task[AnalysisResult] | None = None
    # Set for a run that records its samples; None for one that draws them live instead.
    recorder: SampleRecorder | None = None
    status: str = "PENDING"
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    started_at: dt.datetime | None = None
    ends_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    result: AnalysisResult | None = None
    error: str | None = None
    parent_job_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    # Set the moment a stop is asked for, so a cancel that arrives before the session is
    # built is not lost.
    stop_requested: bool = False

    @property
    def progress(self) -> float:
        if self.status in ("COMPLETED", "CANCELLED", "FAILED"):
            return 1.0
        return self.session.progress if self.session else 0.0

    @property
    def elapsed_s(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or dt.datetime.now(dt.UTC)
        return (end - self.started_at).total_seconds()

    @property
    def remaining_s(self) -> float:
        if self.ends_at is None:
            return 0.0
        return max(0.0, (self.ends_at - dt.datetime.now(dt.UTC)).total_seconds())

    def summary(self) -> dict[str, Any]:
        verdict = self.result.verdict.as_dict() if self.result else None
        counts = (
            self.session.collector.counts()
            if self.session
            else (self.result.verdict.counts if self.result else {})
        )
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "channel_name": self.channel_name,
            "urls": {k: _mask(v) for k, v in self.urls.items()},
            "options": self.options.public(),
            "progress": self.progress,
            "elapsed_s": self.elapsed_s,
            "remaining_s": self.remaining_s,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "counts": counts,
            "verdict": verdict,
            "error": self.error,
            "parent_job_id": self.parent_job_id,
            **self.extra,
        }


def _mask(url: str | None) -> str | None:
    """Mask token values for display; the appendix carries the full URL."""
    if not url:
        return url
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    if not parts.query:
        return url
    masked = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if len(value) > 12 and key.lower() in (
            "hdnts",
            "hdnea",
            "token",
            "auth",
            "sig",
            "signature",
            "aws.sessionid",
            "sessionid",
        ):
            masked.append((key, value[:4] + "…" + value[-4:]))
        else:
            masked.append((key, value))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(masked, doseq=True), parts.fragment)
    )


class JobManager:
    """One instance per process."""

    def __init__(self) -> None:
        self.jobs: dict[str, JobHandle] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self._started = False
        self._on_result: ResultHandler | None = None
        self._event_sink: Callable[[str, str, dict[str, Any]], Awaitable[None]] | None = None
        self._retention_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._started:
            return
        settings = get_settings()
        self._semaphore = asyncio.Semaphore(settings.rba_max_concurrent_jobs)
        self._started = True
        self._retention_task = asyncio.create_task(self._retention_loop(), name="rba:retention")

    async def shutdown(self) -> None:
        """Stop every running job so the process exits cleanly and reports stay writable."""
        if self._retention_task is not None:
            self._retention_task.cancel()
        for handle in list(self.jobs.values()):
            if handle.status in ("RUNNING", "PENDING", "CANCELLING"):
                handle.stop_requested = True
                if handle.session is not None:
                    handle.session.stop()
        running = [h.task for h in self.jobs.values() if h.task and not h.task.done()]
        if running:
            await asyncio.wait(running, timeout=25)
        self._started = False

    def set_result_handler(self, handler: ResultHandler) -> None:
        self._on_result = handler

    def set_event_sink(self, sink: Callable[[str, str, dict[str, Any]], Awaitable[None]]) -> None:
        self._event_sink = sink

    # -- job lifecycle ---------------------------------------------------------

    async def submit(
        self,
        *,
        job_type: JobType,
        playback_url: str,
        origin_url: str | None = None,
        cdn_url: str | None = None,
        ssai_url: str | None = None,
        channel_name: str | None = None,
        options: SessionOptions | None = None,
        job_id: str | None = None,
        parent_job_id: str | None = None,
        persist: bool = True,
    ) -> JobHandle:
        handle = JobHandle(
            id=job_id or uuid.uuid4().hex,
            type=job_type,
            channel_name=channel_name or "(unnamed channel)",
            urls={
                "playback_url": playback_url,
                "origin_url": origin_url,
                "cdn_url": cdn_url,
                "ssai_url": ssai_url,
            },
            options=options or SessionOptions(),
            parent_job_id=parent_job_id,
        )
        handle.ends_at = handle.created_at + dt.timedelta(seconds=handle.options.duration_s)
        self.jobs[handle.id] = handle

        if persist:
            await self._persist_job(handle)

        handle.task = asyncio.create_task(self._run(handle), name=f"rba:{handle.type}:{handle.id}")
        return handle

    async def _run(self, handle: JobHandle) -> AnalysisResult:
        assert self._semaphore is not None, "job manager is not started"
        async with self._semaphore:
            handle.started_at = dt.datetime.now(dt.UTC)
            handle.ends_at = handle.started_at + dt.timedelta(seconds=handle.options.duration_s)

            # A run nobody is watching has to write its polls and fetches down, or it leaves
            # behind findings and nothing to look at. `record_evidence` is already true for
            # aging and false for realtime, which draws its charts from the socket instead.
            recorder = SampleRecorder(handle.id) if handle.options.record_evidence else None
            handle.recorder = recorder

            async def on_event(kind: str, payload: dict[str, Any]) -> None:
                if recorder is not None:
                    await recorder.observe(kind, payload)
                if self._event_sink is not None:
                    await self._event_sink(handle.id, kind, payload)

            session = AnalysisSession(
                session_id=handle.id,
                playback_url=handle.urls["playback_url"] or "",
                origin_url=handle.urls.get("origin_url"),
                cdn_url=handle.urls.get("cdn_url"),
                ssai_url=handle.urls.get("ssai_url"),
                channel_name=handle.channel_name,
                options=handle.options,
                thresholds=get_thresholds(),
                on_event=on_event,
            )
            handle.session = session

            # The session exists before the job is announced as running, and a stop that
            # arrived while it was being built is honoured here. Without both, a cancel in
            # the first moments of a job reaches a handle with no session and does nothing.
            if handle.stop_requested:
                session.stop()
            handle.status = "RUNNING"
            await self._persist_job(handle)

            try:
                result = await session.run()
                handle.result = result
                handle.status = "CANCELLED" if session._stop.is_set() else "COMPLETED"
            except asyncio.CancelledError:
                handle.status = "CANCELLED"
                raise
            except Exception as exc:
                handle.status = "FAILED"
                handle.error = f"{type(exc).__name__}: {exc}"
                logger.exception("job %s failed", handle.id)
                raise
            finally:
                handle.finished_at = dt.datetime.now(dt.UTC)
                # Before the job row is written: what the run measured in its last seconds is
                # part of the run, and a cancelled or failed one keeps what it got to.
                if recorder is not None:
                    await recorder.flush()
                await self._persist_job(handle)

            await self._persist_result(handle, result)
            if self._on_result is not None:
                await self._on_result(handle, result)
            return result

    def get(self, job_id: str) -> JobHandle | None:
        return self.jobs.get(job_id)

    def list_jobs(self, *, job_type: JobType | None = None) -> list[JobHandle]:
        jobs = [h for h in self.jobs.values() if job_type is None or h.type == job_type]
        return sorted(jobs, key=lambda h: h.created_at, reverse=True)

    def children(self, parent_job_id: str) -> list[JobHandle]:
        return [h for h in self.jobs.values() if h.parent_job_id == parent_job_id]

    async def cancel(self, job_id: str) -> JobHandle | None:
        """Stop a job. The report is still produced from what was collected."""
        handle = self.jobs.get(job_id)
        if handle is None:
            return None
        if handle.session is not None:
            handle.session.stop()
        if handle.task is not None and not handle.task.done():
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(asyncio.shield(handle.task), timeout=30)
        return handle

    async def ingest_player_event(self, job_id: str, payload: dict[str, Any]) -> None:
        handle = self.jobs.get(job_id)
        if handle is None or handle.session is None:
            return
        # Player telemetry arrives from the browser rather than from the engine, so it does
        # not pass the event sink. A session that records its samples records these too.
        if handle.recorder is not None:
            await handle.recorder.observe("player_sample", {"data": payload})
        await handle.session.ingest_player_event(payload)

    # -- persistence -----------------------------------------------------------

    async def _persist_job(self, handle: JobHandle) -> None:
        try:
            async with db_session.session_scope() as session:
                row = await session.get(JobRow, handle.id)
                if row is None:
                    row = JobRow(id=handle.id, type=handle.type)
                    session.add(row)
                row.status = handle.status
                row.channel_name = handle.channel_name
                row.parent_job_id = handle.parent_job_id
                row.playback_url = handle.urls.get("playback_url")
                row.origin_url = handle.urls.get("origin_url")
                row.cdn_url = handle.urls.get("cdn_url")
                row.ssai_url = handle.urls.get("ssai_url")
                row.params = handle.options.public()
                row.progress = handle.progress
                row.started_at = handle.started_at
                row.ends_at = handle.ends_at
                row.finished_at = handle.finished_at
                row.error = handle.error
                if handle.result is not None:
                    row.verdict = handle.result.verdict.as_dict()
        except Exception as exc:
            logger.warning(
                "job %s could not be persisted: %s", handle.id, db_session.describe_error(exc)
            )

    async def _persist_result(self, handle: JobHandle, result: AnalysisResult) -> None:
        try:
            async with db_session.session_scope() as session:
                for finding in result.findings:
                    session.add(
                        FindingRow(
                            job_id=handle.id,
                            rule_id=finding.rule.id,
                            severity=finding.severity.value,
                            layer=finding.rule.layer,
                            stream_layer=finding.stream_layer.value,
                            owner=finding.owner.value,
                            variant=finding.variant,
                            title=finding.rule.title,
                            detail=finding.detail,
                            root_cause=finding.rule.root_cause,
                            fix=finding.rule.fix,
                            rebuffer_impact=finding.rule.rebuffer_impact.value,
                            count=finding.count,
                            first_seen=finding.first_seen,
                            last_seen=finding.last_seen,
                            evidence=finding.evidence,
                            layer_presence=finding.layer_presence,
                        )
                    )
                for incident in result.incidents:
                    session.add(
                        IncidentRow(
                            job_id=handle.id,
                            variant=incident.variant,
                            kind=incident.kind,
                            started_at=incident.started_at,
                            ended_at=incident.ended_at,
                            duration_s=incident.duration_s,
                            cause_finding_ids=incident.cause_rule_ids,
                            cause_chain=incident.cause_chain,
                            detail=incident.detail,
                        )
                    )
                for key, vpb_result in result.vpb_results.items():
                    for point in vpb_result.points[-2000:]:
                        session.add(
                            VirtualBufferSample(
                                job_id=handle.id,
                                ts=point.at,
                                variant=key,
                                level_s=point.level_s,
                                state=point.state.value,
                            )
                        )
        except Exception as exc:
            logger.warning(
                "result for %s could not be persisted: %s", handle.id, type(exc).__name__
            )

    async def resume_persisted_jobs(self) -> int:
        """Restart aging and bulk jobs that were running when the process last stopped."""
        resumed = 0
        try:
            async with db_session.session_scope() as session:
                rows = (
                    (
                        await session.execute(
                            select(JobRow).where(
                                JobRow.status.in_(["RUNNING", "PENDING"]),
                                JobRow.type.in_(["aging", "bulk"]),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                now = dt.datetime.now(dt.UTC)
                for row in rows:
                    ends_at = row.ends_at
                    if ends_at is not None and ends_at.tzinfo is None:
                        ends_at = ends_at.replace(tzinfo=dt.UTC)
                    remaining = (ends_at - now).total_seconds() if ends_at else 0.0
                    if remaining <= 5:
                        row.status = "COMPLETED"
                        row.finished_at = now
                        continue
                    params = dict(row.params or {})
                    params["duration_s"] = remaining
                    options = _options_from_params(params)
                    await self.submit(
                        job_type=row.type,
                        playback_url=row.playback_url or "",
                        origin_url=row.origin_url,
                        cdn_url=row.cdn_url,
                        ssai_url=row.ssai_url,
                        channel_name=row.channel_name,
                        options=options,
                        job_id=row.id,
                        parent_job_id=row.parent_job_id,
                        persist=False,
                    )
                    resumed += 1
        except Exception as exc:
            logger.warning(
                "persisted jobs could not be resumed: %s", db_session.describe_error(exc)
            )
        if resumed:
            logger.info("resumed %d job(s) after restart", resumed)
        return resumed

    # -- retention -------------------------------------------------------------

    async def _retention_loop(self) -> None:
        """Purge raw samples past the retention window; findings and reports are kept."""
        while True:
            try:
                await asyncio.sleep(3600)
                await self.purge_old_samples()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("retention pass failed: %s", type(exc).__name__)

    async def purge_old_samples(self) -> int:
        from sqlalchemy import delete

        settings = get_settings()
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=settings.rba_sample_retention_days)
        removed = 0
        async with db_session.session_scope() as session:
            for model in (PlaylistSample, SegmentSample, VirtualBufferSample):
                result = await session.execute(delete(model).where(model.ts < cutoff))
                removed += int(getattr(result, "rowcount", 0) or 0)

            # Snapshots kept around an incident are pinned and survive the purge.
            snapshot_result = await session.execute(
                delete(PlaylistSnapshot).where(
                    PlaylistSnapshot.ts < cutoff, PlaylistSnapshot.pinned.is_(False)
                )
            )
            removed += int(getattr(snapshot_result, "rowcount", 0) or 0)
        return removed


def _options_from_params(params: dict[str, Any]) -> SessionOptions:
    return SessionOptions(
        duration_s=float(params.get("duration_s", 300.0)),
        ua_profile=str(params.get("ua_profile", "tizen5")),
        check_sets=tuple(params.get("check_sets") or ("baseline",)),
        renditions=tuple(params["renditions"]) if params.get("renditions") else None,
        record_evidence=bool(params.get("record_evidence", False)),
        vpb_mode=params.get("vpb_mode"),
        nth_segment_sampling=params.get("nth_segment_sampling"),
        snapshot_mode=bool(params.get("snapshot_mode", False)),
    )


job_manager = JobManager()
