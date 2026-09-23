"""CASCADA error data: one channel's playback errors, this week and the week before.

The call is the rebuffering call with one parameter changed — `target_metrics[]=error_count`
— and the answer has the same shape: per-minute rows tagged `origin` for the requested window
and `comparison` for the seven days before it. Every figure stated here comes from the
`origin` rows; the `comparison` rows feed the previous-week overlay and the previous-week
average, and nothing else.

What differs is the quantity. `errors` is a **count** of playback errors in the minute,
summed over every device that reported one, in the unit CASCADA calls `times`. A count is
summed as well as averaged, because "how many errors this week" is a question a percentage
never answers. And a count scales with the audience, so there is no error threshold by
default: `cascada_error_threshold_per_min` is 0 until a team sets one, and at 0 no minute is
marked and no channel is called above or below anything.

The window is stored in `cascada_error_samples`, which is a cache: dropping it loses nothing
but the need to fetch again. The verdict against the threshold is recomputed on every read.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.cascada import client
from app.cascada.auth import CascadaAuth, resolve_auth
from app.cascada.series import ERRORS, ChannelSeries, Point, Window
from app.cascada.store import points_from
from app.config import Thresholds, get_thresholds
from app.db import session as db_session
from app.db.models import CascadaErrorSample
from app.net.fetcher import Fetcher

logger = logging.getLogger(__name__)


def threshold_for(thresholds: Thresholds) -> float | None:
    """The configured errors-per-minute threshold, or None when none is set."""
    value = thresholds.cascada_error_threshold_per_min
    return value if value > 0 else None


def is_above(value: float | None, threshold: float | None) -> bool:
    """Whether one minute's error count is above the threshold. No threshold, no breach."""
    if value is None or threshold is None:
        return False
    return value > threshold


def _average(points: list[Point]) -> float | None:
    """The mean of the minutes that were measured. A gap is skipped, never counted as zero."""
    values = [p.value for p in points if p.value is not None]
    if not values:
        return None
    return sum(values) / len(values)


@dataclass(slots=True)
class ErrorStats:
    """What the modal and the report state about one channel's errors, from `origin` alone."""

    average_per_min: float | None
    max_per_min: float | None
    max_at: dt.datetime | None
    total: float | None
    minutes_counted: int
    minutes_missing: int
    previous_week_average_per_min: float | None
    threshold_per_min: float | None
    minutes_above: int
    percent_time_above: float | None
    above_threshold: bool

    @property
    def week_over_week_delta_per_min(self) -> float | None:
        """This week's average less last week's, in errors per minute."""
        if self.average_per_min is None or self.previous_week_average_per_min is None:
            return None
        return self.average_per_min - self.previous_week_average_per_min

    @property
    def week_over_week_change_pct(self) -> float | None:
        """The same change relative to last week, so a large channel and a small one compare."""
        delta = self.week_over_week_delta_per_min
        previous = self.previous_week_average_per_min
        if delta is None or not previous:
            return None
        return delta / previous * 100

    def as_dict(self) -> dict[str, Any]:
        return {
            "average_per_min": self.average_per_min,
            "max_per_min": self.max_per_min,
            "max_at": self.max_at.isoformat() if self.max_at else None,
            "total": self.total,
            "minutes_counted": self.minutes_counted,
            "minutes_missing": self.minutes_missing,
            "previous_week_average_per_min": self.previous_week_average_per_min,
            "week_over_week_delta_per_min": self.week_over_week_delta_per_min,
            "week_over_week_change_pct": self.week_over_week_change_pct,
            "threshold_per_min": self.threshold_per_min,
            "minutes_above": self.minutes_above,
            "percent_time_above": self.percent_time_above,
            "above_threshold": self.above_threshold,
        }


def summarise(origin: list[Point], comparison: list[Point], thresholds: Thresholds) -> ErrorStats:
    """The figures for one channel, from its current-week rows.

    As with rebuffering, a channel is above the threshold on its **average**, never on one
    minute's spike.
    """
    threshold = threshold_for(thresholds)
    measured = [p for p in origin if p.value is not None]
    average = _average(origin)
    peak = max(measured, key=lambda p: p.value or 0.0) if measured else None
    above = sum(1 for p in measured if is_above(p.value, threshold))

    return ErrorStats(
        average_per_min=average,
        max_per_min=peak.value if peak else None,
        max_at=peak.at if peak else None,
        total=sum(p.value or 0.0 for p in measured) if measured else None,
        minutes_counted=len(measured),
        minutes_missing=len(origin) - len(measured),
        previous_week_average_per_min=_average(comparison),
        threshold_per_min=threshold,
        minutes_above=above,
        percent_time_above=(above / len(measured) * 100) if measured and threshold else None,
        above_threshold=is_above(average, threshold),
    )


@dataclass(slots=True)
class ErrorWindow:
    """One channel's measured error window, whether it was just fetched or read back."""

    service_id: str
    channel_name: str
    country: str
    window: Window
    unit: str
    stats: ErrorStats
    origin: list[Point]
    comparison: list[Point]
    fetched_at: dt.datetime
    truncated: bool
    cached: bool = False

    @property
    def age_minutes(self) -> float:
        return (dt.datetime.now(dt.UTC) - self.fetched_at).total_seconds() / 60

    def as_dict(self) -> dict[str, Any]:
        """The whole window, series included, as the modal reads it."""
        return {
            "metric": ERRORS.id,
            "unit": self.unit,
            "service_id": self.service_id,
            "channel_name": self.channel_name,
            "country": self.country,
            **self.stats.as_dict(),
            "truncated": self.truncated,
            "fetched_at": self.fetched_at.isoformat(),
            "cached": self.cached,
            "window": self.window.as_dict(),
            "origin": [p.as_dict() for p in self.origin],
            "comparison": [p.as_dict() for p in self.comparison],
        }


def to_window(
    *,
    service_id: str,
    channel_name: str,
    country: str,
    series: ChannelSeries,
    window: Window,
    thresholds: Thresholds,
) -> ErrorWindow:
    """A freshly fetched series as the shape everything downstream reads."""
    return ErrorWindow(
        service_id=service_id,
        channel_name=channel_name,
        country=country,
        window=window,
        unit=series.unit,
        stats=summarise(series.origin, series.comparison, thresholds),
        origin=series.origin,
        comparison=series.comparison,
        fetched_at=dt.datetime.now(dt.UTC),
        truncated=series.truncated,
        cached=False,
    )


# -- the store ---------------------------------------------------------------


def _as_window(row: CascadaErrorSample, thresholds: Thresholds) -> ErrorWindow:
    series = row.series if isinstance(row.series, dict) else {}
    origin = points_from(series.get("origin"))
    comparison = points_from(series.get("comparison"))
    fetched = row.fetched_at
    return ErrorWindow(
        service_id=row.service_id,
        channel_name=row.channel_name,
        country=row.country or "",
        window=Window(
            start=dt.datetime.fromtimestamp(row.window_from, dt.UTC),
            end=dt.datetime.fromtimestamp(row.window_to, dt.UTC),
        ),
        unit=str(series.get("unit") or ERRORS.unit),
        stats=summarise(origin, comparison, thresholds),
        origin=origin,
        comparison=comparison,
        fetched_at=fetched if fetched.tzinfo else fetched.replace(tzinfo=dt.UTC),
        truncated=row.truncated,
        cached=True,
    )


def _matching(service_id: str, window: Window) -> Any:
    return select(CascadaErrorSample).where(
        CascadaErrorSample.service_id == service_id,
        CascadaErrorSample.window_from == int(window.start.timestamp()),
        CascadaErrorSample.window_to == int(window.end.timestamp()),
    )


async def read(service_id: str, window: Window, thresholds: Thresholds) -> ErrorWindow | None:
    """A stored window for this channel, if one is present and still inside its TTL."""
    try:
        async with db_session.session_scope() as session:
            found = (await session.execute(_matching(service_id, window))).scalars().first()
            if found is None:
                return None
            stored = _as_window(found, thresholds)
    except Exception as exc:
        # A cache that cannot be read is a slower call, not a failed one — but it is said.
        logger.warning("the CASCADA error store could not be read: %s", type(exc).__name__)
        return None

    return stored if stored.age_minutes <= thresholds.cascada_cache_ttl_minutes else None


async def write(entry: ErrorWindow) -> None:
    """Store one channel's window, replacing whatever was held for the same window."""
    payload = {
        "unit": entry.unit,
        "origin": [p.as_dict() for p in entry.origin],
        "comparison": [p.as_dict() for p in entry.comparison],
    }
    try:
        async with db_session.session_scope() as session:
            found = (
                (await session.execute(_matching(entry.service_id, entry.window))).scalars().first()
            )
            row = found or CascadaErrorSample(
                service_id=entry.service_id,
                window_from=int(entry.window.start.timestamp()),
                window_to=int(entry.window.end.timestamp()),
            )
            row.channel_name = entry.channel_name
            row.country = entry.country or None
            row.fetched_at = entry.fetched_at
            row.average_per_min = entry.stats.average_per_min
            row.max_per_min = entry.stats.max_per_min
            row.max_at = entry.stats.max_at
            row.total = entry.stats.total
            row.previous_week_average_per_min = entry.stats.previous_week_average_per_min
            row.minutes_counted = entry.stats.minutes_counted
            row.truncated = entry.truncated
            row.series = payload
            if found is None:
                session.add(row)
    except Exception as exc:
        # Failing to cache must never fail the call that produced the measurement.
        logger.warning(
            "the CASCADA error window for %s was not stored: %s",
            entry.service_id,
            type(exc).__name__,
        )


# -- the service -------------------------------------------------------------


async def error_window(
    *,
    service_id: str,
    channel_name: str,
    country: str,
    thresholds: Thresholds | None = None,
    window: Window | None = None,
    auth: CascadaAuth | None = None,
    fetcher: Fetcher | None = None,
    refresh: bool = False,
) -> ErrorWindow:
    """One channel's error window, from the store when it is fresh, else from CASCADA.

    The window is the rebuffering window, `client.window_for`, so the two views of one channel
    always cover the same minutes.
    """
    limits = thresholds or get_thresholds()
    span = window or client.window_for(limits)

    if not refresh:
        cached = await read(service_id, span, limits)
        if cached is not None:
            return cached

    provider = auth or await resolve_auth()
    series = await client.fetch_series(
        channel_name=channel_name,
        channel_id=service_id,
        auth=provider,
        thresholds=limits,
        window=span,
        fetcher=fetcher,
        metric=ERRORS,
    )
    entry = to_window(
        service_id=service_id,
        channel_name=channel_name,
        country=country,
        series=series,
        window=span,
        thresholds=limits,
    )
    await write(entry)
    return entry
