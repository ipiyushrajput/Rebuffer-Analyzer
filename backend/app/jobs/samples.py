"""Writing a job's playlist polls and segment fetches down as it runs.

Realtime draws its charts from the live socket, so for a session somebody is watching the
measurement never has to be stored. An aging run has no socket anyone is watching — it runs
for days and the operator comes back afterwards — so unless the polls and fetches are written
down as they happen, the run leaves behind its findings and nothing to look at.

The rows are written from the job manager's event sink rather than from the engine: the sink
already receives every `playlist_snapshot` and `segment_result` the engine emits, in the exact
shape the WebSocket sends them, so a second path through the engine would be a second chance
for the two to disagree.

Writes are batched. A seven-day run over six renditions polls a few times a second, and a
database round trip per poll would put the analyzer's own latency into the measurement it is
taking. Rows accumulate in memory and go out in one statement per flush; a flush that fails is
logged with its traceback and the run carries on, because a measurement that cannot be
recorded must not stop the measuring.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
from typing import Any

from app.db import session as db_session
from app.db.models import PlayerSample, PlaylistSample, SegmentSample

logger = logging.getLogger(__name__)

# Rows held before a flush. Large enough that a busy run flushes a few times a minute rather
# than a few times a second, small enough that a crash loses seconds of measurement.
BATCH_ROWS = 250

# A flush happens on this interval even when the batch is not full, so a quiet rung's samples
# reach the database while the run is still going rather than only at the end.
FLUSH_INTERVAL_S = 20.0


def _moment(raw: Any) -> dt.datetime:
    """The timestamp the engine stamped on the sample, not the time it reached here."""
    if isinstance(raw, dt.datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=dt.UTC)
    if isinstance(raw, str) and raw:
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return dt.datetime.now(dt.UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return dt.datetime.now(dt.UTC)


def _float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


# What a signed BIGINT holds. The player's numbers come from a browser, so the recorder is
# the boundary that decides what the database is asked to store: handing the driver a value
# the column cannot take fails the whole batch, not just the row.
BIGINT_MAX = 2**63 - 1
BIGINT_MIN = -(2**63)


def _bigint(value: Any) -> int | None:
    """One integer a BIGINT column will accept, or None with the reason logged.

    A value past the range is dropped rather than clamped: a clamped number is a measurement
    that reads as real and is not, and this project reports what it measured or says it could
    not.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    whole = int(value)
    if BIGINT_MIN <= whole <= BIGINT_MAX:
        return whole
    logger.warning("a player sample reported %s, past what the column stores; dropped", whole)
    return None


def playlist_row(job_id: str, layer: str, data: dict[str, Any]) -> PlaylistSample:
    """One playlist poll, from the payload the socket carries."""
    return PlaylistSample(
        job_id=job_id,
        ts=_moment(data.get("at")),
        stream_layer=layer,
        variant=str(data.get("variant") or ""),
        msn=int(data.get("msn") or 0),
        last_msn=int(data.get("last_msn") or 0),
        dsn=int(data.get("dsn") or 0),
        seg_count=int(data.get("segments") or 0),
        target_duration=_float(data.get("target_duration")),
        window_s=float(data.get("window_s") or 0.0),
        freshness_s=float(data.get("freshness_s") or 0.0),
        http_status=int(data.get("status") or 0),
        ttfb_ms=_float(data.get("ttfb_ms")),
        total_ms=float(data.get("total_ms") or 0.0),
        bytes=int(data.get("bytes") or 0),
        state=str(data.get("state") or "UNKNOWN")[:16],
    )


def segment_row(job_id: str, layer: str, data: dict[str, Any]) -> SegmentSample:
    """One segment fetch, from the payload the socket carries."""
    uri = str(data.get("uri") or "")
    return SegmentSample(
        job_id=job_id,
        ts=_moment(data.get("at")),
        stream_layer=layer,
        variant=str(data.get("variant") or ""),
        msn=int(data.get("msn") or 0),
        uri_hash=hashlib.sha1(uri.encode("utf-8", "replace")).hexdigest(),
        uri=uri,
        bytes=int(data.get("bytes") or 0),
        duration_declared=_float(data.get("declared_duration")),
        duration_actual=_float(data.get("actual_duration")),
        download_ms=float(data.get("download_ms") or 0.0),
        ttfb_ms=_float(data.get("ttfb_ms")),
        http_status=int(data.get("status") or 0),
        measured_kbps=_float(data.get("measured_kbps")),
        av_skew_ms=_float(data.get("av_skew_ms")),
        detail={
            "container": data.get("container") or "",
            "starts_with_keyframe": data.get("starts_with_keyframe"),
        },
    )


def player_row(job_id: str, data: dict[str, Any]) -> PlayerSample:
    """One report from the browser player, which only a watched session produces.

    `bitrate` and `bandwidth_bps` are different quantities and are kept apart: the first is
    the rung the player switched to, the second is what it measured the network doing. They
    shared a column until a 2.8 Gbps throughput estimate overflowed it and took a whole batch
    of samples with it.
    """
    return PlayerSample(
        job_id=job_id,
        ts=_moment(data.get("at")),
        event=str(data.get("event") or "")[:32],
        buffer_s=_float(data.get("buffer_s")),
        level=int(data["level"]) if isinstance(data.get("level"), int) else None,
        bitrate=_bigint(data.get("bitrate")),
        bandwidth_bps=_bigint(data.get("bandwidth_bps")),
        dropped_frames=int(data.get("dropped_frames") or 0),
        stall_duration_s=_float(data.get("stall_duration_s")),
    )


class SampleRecorder:
    """Buffers one job's samples and writes them down in batches."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self._rows: list[Any] = []
        self._lock = asyncio.Lock()
        self._last_flush = dt.datetime.now(dt.UTC)
        self.written = 0
        self.failed_flushes = 0
        # Rows the database refused one at a time, after a batch failed. Counted separately
        # from a failed flush: a flush that salvages 249 of 250 is not the same event as one
        # that loses everything, and `sampling_state` reports the difference.
        self.rows_dropped = 0

    async def observe(self, kind: str, payload: dict[str, Any]) -> None:
        """Turn one engine event into a row, and flush when the batch is full or stale."""
        data = payload.get("data")
        if not isinstance(data, dict):
            return
        layer = str(payload.get("layer") or "PLAYBACK")

        if kind == "playlist_snapshot":
            row: Any = playlist_row(self.job_id, layer, data)
        elif kind == "segment_result":
            row = segment_row(self.job_id, layer, data)
        elif kind == "player_sample":
            row = player_row(self.job_id, data)
        else:
            return

        async with self._lock:
            self._rows.append(row)
            due = (dt.datetime.now(dt.UTC) - self._last_flush).total_seconds() >= FLUSH_INTERVAL_S
            if len(self._rows) < BATCH_ROWS and not due:
                return
            pending, self._rows = self._rows, []
            self._last_flush = dt.datetime.now(dt.UTC)

        await self._write(pending)

    async def flush(self) -> None:
        """Write whatever is buffered. Called when the job finishes."""
        async with self._lock:
            pending, self._rows = self._rows, []
            self._last_flush = dt.datetime.now(dt.UTC)
        await self._write(pending)

    async def _write(self, rows: list[Any]) -> None:
        if not rows:
            return
        try:
            async with db_session.session_scope() as session:
                session.add_all(rows)
            self.written += len(rows)
        except Exception:
            # The run keeps measuring: samples are evidence, not the measurement itself, and
            # a database that refuses them must not take the analysis down with it.
            self.failed_flushes += 1
            logger.exception("%d sample(s) for job %s could not be written", len(rows), self.job_id)
            await self._write_individually(rows)

    async def _write_individually(self, rows: list[Any]) -> None:
        """Retry a failed batch row by row, keeping every row the database will take.

        One row the column cannot hold used to discard the whole batch with it — a single
        malformed player sample cost 137 good measurements. A batch is a transport
        optimisation, not a unit of meaning, so a row that fails should lose only itself.
        """
        if len(rows) <= 1:
            return
        kept = 0
        for row in rows:
            try:
                async with db_session.session_scope() as session:
                    session.add(row)
                kept += 1
            except Exception as exc:
                self.rows_dropped += 1
                logger.warning(
                    "one %s sample for job %s was refused and dropped: %s",
                    type(row).__name__,
                    self.job_id,
                    db_session.describe_error(exc),
                )
        self.written += kept
        logger.info(
            "%d of %d sample(s) for job %s were salvaged after the batch failed",
            kept,
            len(rows),
            self.job_id,
        )
