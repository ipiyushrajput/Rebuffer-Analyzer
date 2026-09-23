"""Where a fetched CASCADA window is kept.

A country scan is one call per channel; a large country is several hundred. Storing each
result means the scan is paid for once: reopening a channel's modal, re-running the scan
inside the TTL, downloading the country report after a restart and a second analyzer instance
serving the same report all read from here instead of from CASCADA.

The row is a cache and nothing depends on it surviving: dropping the table loses no analysis,
only the need to fetch again.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.cascada.series import ChannelSeries, Point, Stats, Window
from app.db import session as db_session
from app.db.models import CascadaSample

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ChannelWindow:
    """One channel's measured window, whether it was just fetched or read back."""

    service_id: str
    channel_name: str
    country: str
    window: Window
    stats: Stats
    origin: list[Point]
    comparison: list[Point]
    fetched_at: dt.datetime
    truncated: bool
    # Empty when the window was fetched in this request; set when it was read from the store.
    cached: bool = False

    @property
    def age_minutes(self) -> float:
        return (dt.datetime.now(dt.UTC) - self.fetched_at).total_seconds() / 60

    def row(self) -> dict[str, Any]:
        """The channel as the listing and the country report state it, without the series."""
        return {
            "service_id": self.service_id,
            "channel_name": self.channel_name,
            "country": self.country,
            **self.stats.as_dict(),
            "truncated": self.truncated,
            "fetched_at": self.fetched_at.isoformat(),
            "cached": self.cached,
        }

    def as_dict(self) -> dict[str, Any]:
        """The whole window, series included, as the modal reads it."""
        return {
            **self.row(),
            "window": self.window.as_dict(),
            "origin": [p.as_dict() for p in self.origin],
            "comparison": [p.as_dict() for p in self.comparison],
        }


def points_from(raw: Any) -> list[Point]:
    """Stored `{at, value}` points back into `Point`s, skipping anything malformed."""
    points: list[Point] = []
    if not isinstance(raw, list):
        return points
    for item in raw:
        if not isinstance(item, dict):
            continue
        at = item.get("at")
        if not isinstance(at, str):
            continue
        try:
            moment = dt.datetime.fromisoformat(at)
        except ValueError:
            continue
        value = item.get("value")
        points.append(
            Point(
                at=moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC),
                value=float(value) if isinstance(value, int | float) else None,
            )
        )
    return points


def _as_window(row: CascadaSample) -> ChannelWindow:
    series = row.series if isinstance(row.series, dict) else {}
    fetched = row.fetched_at
    stats = Stats(
        average_pct=row.average_pct,
        max_pct=row.max_pct,
        max_at=row.max_at,
        minutes_above=row.minutes_above,
        minutes_counted=row.minutes_counted,
        minutes_missing=int(series.get("minutes_missing") or 0),
        percent_time_above=(
            row.minutes_above / row.minutes_counted * 100 if row.minutes_counted else None
        ),
        previous_week_average_pct=row.previous_week_average_pct,
        threshold_pct=float(series.get("threshold_pct") or 0.0),
        above_threshold=row.above_threshold,
    )
    return ChannelWindow(
        service_id=row.service_id,
        channel_name=row.channel_name,
        country=row.country or "",
        window=Window(
            start=dt.datetime.fromtimestamp(row.window_from, dt.UTC),
            end=dt.datetime.fromtimestamp(row.window_to, dt.UTC),
        ),
        stats=stats,
        origin=points_from(series.get("origin")),
        comparison=points_from(series.get("comparison")),
        fetched_at=fetched if fetched.tzinfo else fetched.replace(tzinfo=dt.UTC),
        truncated=row.truncated,
        cached=True,
    )


def to_window(
    *,
    service_id: str,
    channel_name: str,
    country: str,
    series: ChannelSeries,
    stats: Stats,
    window: Window,
) -> ChannelWindow:
    """A freshly fetched series as the shape everything downstream reads."""
    return ChannelWindow(
        service_id=service_id,
        channel_name=channel_name,
        country=country,
        window=window,
        stats=stats,
        origin=series.origin,
        comparison=series.comparison,
        fetched_at=dt.datetime.now(dt.UTC),
        truncated=series.truncated,
        cached=False,
    )


async def read(service_id: str, window: Window, ttl_minutes: int) -> ChannelWindow | None:
    """A stored window for this channel, if one is present and still inside its TTL."""
    try:
        async with db_session.session_scope() as session:
            found = (
                (
                    await session.execute(
                        select(CascadaSample).where(
                            CascadaSample.service_id == service_id,
                            CascadaSample.window_from == int(window.start.timestamp()),
                            CascadaSample.window_to == int(window.end.timestamp()),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if found is None:
                return None
            stored = _as_window(found)
    except Exception as exc:
        # A cache that cannot be read is a slower call, not a failed one — but it is said.
        logger.warning("the CASCADA store could not be read: %s", type(exc).__name__)
        return None

    return stored if stored.age_minutes <= ttl_minutes else None


async def read_country(country: str, window: Window, ttl_minutes: int) -> dict[str, ChannelWindow]:
    """Every stored window for one country and one measurement window, keyed by service id."""
    try:
        async with db_session.session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(CascadaSample).where(
                            CascadaSample.country == country,
                            CascadaSample.window_from == int(window.start.timestamp()),
                            CascadaSample.window_to == int(window.end.timestamp()),
                        )
                    )
                )
                .scalars()
                .all()
            )
            stored = [_as_window(row) for row in rows]
    except Exception as exc:
        logger.warning("the CASCADA store could not be read: %s", type(exc).__name__)
        return {}

    return {w.service_id: w for w in stored if w.age_minutes <= ttl_minutes}


async def write(entry: ChannelWindow) -> None:
    """Store one channel's window, replacing whatever was held for the same window."""
    payload = {
        "origin": [p.as_dict() for p in entry.origin],
        "comparison": [p.as_dict() for p in entry.comparison],
        "minutes_missing": entry.stats.minutes_missing,
        "threshold_pct": entry.stats.threshold_pct,
    }
    try:
        async with db_session.session_scope() as session:
            found = (
                (
                    await session.execute(
                        select(CascadaSample).where(
                            CascadaSample.service_id == entry.service_id,
                            CascadaSample.window_from == int(entry.window.start.timestamp()),
                            CascadaSample.window_to == int(entry.window.end.timestamp()),
                        )
                    )
                )
                .scalars()
                .first()
            )
            row = found or CascadaSample(
                service_id=entry.service_id,
                window_from=int(entry.window.start.timestamp()),
                window_to=int(entry.window.end.timestamp()),
            )
            row.channel_name = entry.channel_name
            row.country = entry.country or None
            row.fetched_at = entry.fetched_at
            row.average_pct = entry.stats.average_pct
            row.max_pct = entry.stats.max_pct
            row.max_at = entry.stats.max_at
            row.minutes_above = entry.stats.minutes_above
            row.minutes_counted = entry.stats.minutes_counted
            row.previous_week_average_pct = entry.stats.previous_week_average_pct
            row.above_threshold = entry.stats.above_threshold
            row.truncated = entry.truncated
            row.series = payload
            if found is None:
                session.add(row)
    except Exception as exc:
        # Failing to cache must never fail the call that produced the measurement.
        logger.warning(
            "the CASCADA window for %s was not stored: %s", entry.service_id, type(exc).__name__
        )
