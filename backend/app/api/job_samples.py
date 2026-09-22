"""The stored samples of one job, shaped for the charts that already exist.

Realtime's charts are fed live from the WebSocket. An aging run has no socket to watch — it
runs for days and nobody is looking — so its charts have to come from what was written down.
Every field they need is already in `samples_playlist`, `samples_segment`, `samples_player`
and `virtual_buffer`; this module reads those and hands back exactly the shapes the chart
components take, so the same components render an aging run with no change to them.

Two properties matter as much as the data.

**A week is too much to send whole.** A seven-day run at a poll every few seconds is hundreds
of thousands of rows. The range narrows it and `max_points` thins it, and the thinning is
even — every Nth row — so the shape of the series survives.

**A gap stays a gap.** A minute with no sample is absent from the series, never a zero: a zero
would claim a measurement that was never taken, and the charts draw it as a real value.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PlayerSample, PlaylistSample, SegmentSample, VirtualBufferSample

T = TypeVar("T")

# What one request will return per series before thinning kicks in. Enough for a week to read
# at a glance; the raw rows stay in the database for anyone who needs them.
DEFAULT_MAX_POINTS = 2000
HARD_LIMIT = 200_000


def _utc(moment: dt.datetime) -> dt.datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


# A rung identifier is built from the master playlist as `v1080p@5000k` — height and the
# declared BANDWIDTH in kbit/s. See `app/hls/playlist.py::Variant.variant_id`.
_RUNG_RATE = re.compile(r"@(\d+)k$")


def declared_bandwidth(variants: Iterable[str]) -> dict[str, int | None]:
    """Each rung's declared `BANDWIDTH`, in bit/s, read back from its identifier.

    The ladder table is not among the stored samples, but the rung identifier is generated
    from the master playlist and carries the declared rate, so the number the bitrate chart
    compares the measurement against is recovered exactly rather than guessed. A rung that
    declares no rate, and a media-only playlist that has no master to declare one, carry
    `null` — the chart then draws the measurement alone instead of a zero nobody measured.
    """
    rates: dict[str, int | None] = {}
    for variant in variants:
        if variant in rates:
            continue
        match = _RUNG_RATE.search(variant)
        kbps = int(match.group(1)) if match else 0
        rates[variant] = kbps * 1000 if kbps else None
    return rates


def thin(rows: list[T], max_points: int) -> list[T]:
    """Every Nth row, keeping the first and the last.

    Even thinning rather than clever thinning: the charts draw timing, status and sequence
    numbers, and an algorithm that chose "interesting" points would quietly drop the flat
    stretches that show a stream behaving.
    """
    if max_points <= 0 or len(rows) <= max_points:
        return rows
    step = len(rows) / max_points
    picked = [rows[int(index * step)] for index in range(max_points)]
    if rows[-1] is not picked[-1]:
        picked[-1] = rows[-1]
    return picked


def _snapshot(row: PlaylistSample) -> dict[str, Any]:
    """One playlist poll, as `PlaylistSnapshotData` in the frontend."""
    return {
        "at": _utc(row.ts).isoformat(),
        "variant": row.variant,
        "url": "",
        "status": row.http_status,
        "msn": row.msn,
        "last_msn": row.last_msn,
        "dsn": row.dsn,
        "segments": row.seg_count,
        "window_s": row.window_s,
        "target_duration": row.target_duration,
        "ttfb_ms": row.ttfb_ms,
        "total_ms": row.total_ms,
        "bytes": row.bytes,
        "headers": {},
        # Not in the live message, but the freshness chart reads it from the sample.
        "freshness_s": row.freshness_s,
        "state": row.state,
    }


def _segment(row: SegmentSample) -> dict[str, Any]:
    """One segment fetch, as `SegmentResultData` in the frontend."""
    detail = row.detail or {}
    return {
        "variant": row.variant,
        "msn": row.msn,
        "uri": row.uri,
        "at": _utc(row.ts).isoformat(),
        "status": row.http_status,
        "bytes": row.bytes,
        "download_ms": row.download_ms,
        "ttfb_ms": row.ttfb_ms,
        "declared_duration": row.duration_declared,
        "actual_duration": row.duration_actual,
        "measured_kbps": row.measured_kbps,
        "av_skew_ms": row.av_skew_ms,
        "container": str(detail.get("container", "")),
        "starts_with_keyframe": detail.get("starts_with_keyframe"),
    }


def _player(row: PlayerSample) -> dict[str, Any]:
    return {
        "at": _utc(row.ts).isoformat(),
        "event": row.event,
        "buffer_s": row.buffer_s,
        "level": row.level,
        # The rung being played, and separately what the player measured the network doing.
        # They shared a field until a gigabit throughput estimate drew itself on the
        # played-rung chart as if the player had switched to a rung that does not exist.
        "bitrate": row.bitrate,
        "bandwidth_bps": row.bandwidth_bps,
        "dropped_frames": row.dropped_frames,
        "stall_duration_s": row.stall_duration_s,
    }


async def read_samples(
    session: AsyncSession,
    job_id: str,
    *,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    max_points: int = DEFAULT_MAX_POINTS,
) -> dict[str, Any]:
    """Everything the charts draw for one job over one range."""

    def ranged(query: Any, column: Any) -> Any:
        if start is not None:
            query = query.where(column >= start)
        if end is not None:
            query = query.where(column <= end)
        return query

    snapshots = list(
        (
            await session.execute(
                ranged(
                    select(PlaylistSample)
                    .where(PlaylistSample.job_id == job_id)
                    .order_by(PlaylistSample.ts),
                    PlaylistSample.ts,
                ).limit(HARD_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    segments = list(
        (
            await session.execute(
                ranged(
                    select(SegmentSample)
                    .where(SegmentSample.job_id == job_id)
                    .order_by(SegmentSample.ts),
                    SegmentSample.ts,
                ).limit(HARD_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    players = list(
        (
            await session.execute(
                ranged(
                    select(PlayerSample)
                    .where(PlayerSample.job_id == job_id)
                    .order_by(PlayerSample.ts),
                    PlayerSample.ts,
                ).limit(HARD_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    buffer_rows = list(
        (
            await session.execute(
                ranged(
                    select(VirtualBufferSample)
                    .where(VirtualBufferSample.job_id == job_id)
                    .order_by(VirtualBufferSample.ts),
                    VirtualBufferSample.ts,
                ).limit(HARD_LIMIT)
            )
        )
        .scalars()
        .all()
    )

    vpb: dict[str, list[dict[str, Any]]] = {}
    for sample in thin(buffer_rows, max_points):
        vpb.setdefault(sample.variant, []).append(
            {
                "at": _utc(sample.ts).isoformat(),
                "level_s": sample.level_s,
                "state": sample.state,
            }
        )

    measured = [row.ts for row in (*snapshots, *segments)]
    return {
        "job_id": job_id,
        "range": {
            "from": _utc(start).isoformat() if start else None,
            "to": _utc(end).isoformat() if end else None,
            # What the job actually covers, so a range filter knows its bounds.
            "first_sample": _utc(min(measured)).isoformat() if measured else None,
            "last_sample": _utc(max(measured)).isoformat() if measured else None,
        },
        "counts": {
            "snapshots": len(snapshots),
            "segments": len(segments),
            "player": len(players),
            "virtual_buffer": len(buffer_rows),
        },
        "max_points": max_points,
        "downsampled": any(
            len(rows) > max_points for rows in (snapshots, segments, players, buffer_rows)
        ),
        "snapshots": [_snapshot(row) for row in thin(snapshots, max_points)],
        "segments": [_segment(row) for row in thin(segments, max_points)],
        "declared": declared_bandwidth(row.variant for row in (*snapshots, *segments)),
        "player": [_player(row) for row in thin(players, max_points)],
        "vpb": vpb,
        # An aging run has no browser attached, so it records no player samples. Saying so
        # is what stops the player charts from reading as a measurement of zero.
        "player_note": ""
        if players
        else "An aging run has no player attached, so it records no player samples.",
    }
