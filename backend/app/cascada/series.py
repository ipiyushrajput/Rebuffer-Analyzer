"""Reading a CASCADA rebuffering response.

The response carries two series in one list, told apart by `date_category`:

* `origin` — the window the call asked for, which is the current week.
* `comparison` — the seven days immediately before it, which `compare_with=1WEEK` adds.

Every number the product states — the average, the threshold check, the red row, the country
report — comes from the `origin` rows. The `comparison` rows exist so the chart can lay last
week over this week, and nowhere else. Mixing them would average fourteen days of history
into a figure labelled as this week's.

`rebuffering_ratio` is a **percentage**: a value of 0.159 is 0.159% of viewing time spent
rebuffering. It is not the fraction that `Thresholds.rebuffer_ratio_threshold` holds, where
0.25 means 25%. The comparison therefore goes through `is_above` against
`cascada_rebuffering_threshold_pct`, and that is the only place the operator is applied.

The same endpoint answers for other metrics, and the response shape does not change — only the
key each row carries its value under. `Metric` names that key. The two are not always spelled
alike: a call asking for `target_metrics[]=error_count` is answered with rows keyed `errors`,
so a metric lists every key its value can arrive under, and a response carrying none of them
is an error rather than a week of gaps.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from app.config import Thresholds

ORIGIN = "origin"
COMPARISON = "comparison"

METRIC = "rebuffering_ratio"
EXPECTED_UNIT = "%"

SECONDS_PER_MINUTE = 60


@dataclass(frozen=True, slots=True)
class Metric:
    """One CASCADA metric: what the call asks for and where the answer puts it."""

    id: str
    label: str
    # The value sent as `target_metrics[]`.
    request_key: str
    # Every row key the value can come back under, in the order they are tried.
    response_keys: tuple[str, ...]
    # What the value is measured in when the response does not say.
    unit: str


REBUFFERING = Metric(
    id="rebuffering",
    label="rebuffering",
    request_key=METRIC,
    response_keys=(METRIC,),
    unit=EXPECTED_UNIT,
)

# Asked for as `error_count`, answered as `errors`, in `times`: a count of playback errors in
# the minute, summed over every device that reported one.
ERRORS = Metric(
    id="errors",
    label="error",
    request_key="error_count",
    response_keys=("errors", "error_count"),
    unit="times",
)


class CascadaParseError(ValueError):
    """The response did not carry the series the call asked for."""


@dataclass(frozen=True, slots=True)
class Point:
    """One minute of measurement. `value` is None for a minute CASCADA reported nothing for."""

    at: dt.datetime
    value: float | None

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "value": self.value}


@dataclass(frozen=True, slots=True)
class Window:
    """A half-open measurement window, always in UTC."""

    start: dt.datetime
    end: dt.datetime

    @property
    def minutes(self) -> int:
        """Whole minutes the window spans, counting both ends, as CASCADA reports them."""
        span = (self.end - self.start).total_seconds()
        return max(0, int(span // SECONDS_PER_MINUTE) + 1)

    @property
    def days(self) -> float:
        return (self.end - self.start).total_seconds() / 86400

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "minutes": self.minutes,
            "days": round(self.days, 3),
        }


def _parse_time(raw: Any) -> dt.datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _parse_value(raw: Any) -> float | None:
    """A measured percentage, or None for a minute that carries no measurement.

    A missing or null ratio is a gap, never a zero: a zero would say the channel rebuffered
    for none of that minute, which is a measurement CASCADA did not make.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            return float(raw.strip())
        except ValueError:
            return None
    return None


@dataclass(slots=True)
class ChannelSeries:
    """One channel's two series, and what is known about how complete they are."""

    origin: list[Point]
    comparison: list[Point]
    requested: Window
    reference: Window | None
    unit: str
    # The categories the response carried, so a value nobody has seen before is reported
    # rather than silently dropped into neither series.
    categories: dict[str, int]

    @property
    def measured(self) -> Window | None:
        """The window the `origin` rows actually cover."""
        if not self.origin:
            return None
        return Window(start=self.origin[0].at, end=self.origin[-1].at)

    @property
    def truncated(self) -> bool:
        """True when fewer origin minutes came back than the call asked for.

        `limit` is computed from the window, so this should not fire; when it does, the
        average covers less than the window it is labelled with and the UI says so rather
        than presenting a partial week as a whole one.
        """
        return len(self.origin) < self.requested.minutes

    @property
    def unexpected_categories(self) -> list[str]:
        return sorted(set(self.categories) - {ORIGIN, COMPARISON})

    def as_dict(self) -> dict[str, Any]:
        measured = self.measured
        return {
            "requested_window": self.requested.as_dict(),
            "reference_window": self.reference.as_dict() if self.reference else None,
            "measured_window": measured.as_dict() if measured else None,
            "unit": self.unit,
            "origin_minutes": len(self.origin),
            "comparison_minutes": len(self.comparison),
            "categories": self.categories,
            "unexpected_categories": self.unexpected_categories,
            "truncated": self.truncated,
        }


def _value_key(rows: list[Any], metric: Metric) -> str | None:
    """The key this response carries the metric under, or None when no row carries it."""
    for key in metric.response_keys:
        if any(isinstance(row, dict) and key in row for row in rows):
            return key
    return None


def parse(payload: Any, *, requested: Window, metric: Metric = REBUFFERING) -> ChannelSeries:
    """Split one CASCADA response into its current and previous-week series."""
    if not isinstance(payload, dict):
        raise CascadaParseError(
            f"CASCADA answered with a {type(payload).__name__}, not the expected object "
            "carrying `data`."
        )

    rows = payload.get("data")
    if not isinstance(rows, list):
        raise CascadaParseError(
            "The CASCADA response carries no `data` list. Keys present: "
            f"{', '.join(sorted(str(k) for k in payload)) or 'none'}."
        )

    key = _value_key(rows, metric)
    if key is None and rows:
        # Rows that carry no value under any expected key would read as a window of gaps,
        # which states that CASCADA measured nothing. It measured something else.
        seen = sorted({str(k) for row in rows if isinstance(row, dict) for k in row})
        raise CascadaParseError(
            f"CASCADA returned {len(rows)} row(s) and none carries "
            f"{' or '.join(f'`{k}`' for k in metric.response_keys)}. "
            f"Row keys present: {', '.join(seen) or 'none'}."
        )

    origin: list[Point] = []
    comparison: list[Point] = []
    categories: dict[str, int] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        category = str(row.get("date_category") or "").strip()
        categories[category] = categories.get(category, 0) + 1
        at = _parse_time(row.get("target_time"))
        if at is None:
            continue
        point = Point(at=at, value=_parse_value(row.get(key) if key else None))
        if category == ORIGIN:
            origin.append(point)
        elif category == COMPARISON:
            comparison.append(point)

    origin.sort(key=lambda p: p.at)
    comparison.sort(key=lambda p: p.at)

    unit = metric.unit
    units = payload.get("unit")
    if isinstance(units, dict) and key and isinstance(units.get(key), str):
        unit = units[key]

    reference = None
    block = payload.get("reference_time")
    if isinstance(block, dict):
        start = _parse_time(block.get("start"))
        end = _parse_time(block.get("end"))
        if start and end:
            reference = Window(start=start, end=end)

    return ChannelSeries(
        origin=origin,
        comparison=comparison,
        requested=requested,
        reference=reference,
        unit=unit,
        categories=categories,
    )


def is_above(value: float | None, thresholds: Thresholds) -> bool:
    """Whether one measured percentage is above the threshold.

    The comparison lives here alone, so changing `>` to `>=` — or changing what it compares
    against — is one edit in one place.
    """
    if value is None:
        return False
    return value > thresholds.cascada_rebuffering_threshold_pct


def _average(points: list[Point]) -> float | None:
    """The mean of the minutes that were measured. A gap is skipped, never counted as zero."""
    values = [p.value for p in points if p.value is not None]
    if not values:
        return None
    return sum(values) / len(values)


@dataclass(slots=True)
class Stats:
    """What the table, the modal and the reports state about one channel.

    Every field is computed from the `origin` rows, except `previous_week_average_pct`, which
    is the whole reason the comparison rows are fetched.
    """

    average_pct: float | None
    max_pct: float | None
    max_at: dt.datetime | None
    minutes_above: int
    minutes_counted: int
    minutes_missing: int
    percent_time_above: float | None
    previous_week_average_pct: float | None
    threshold_pct: float
    above_threshold: bool

    @property
    def week_over_week_delta_pct(self) -> float | None:
        """This week's average less last week's, in percentage points."""
        if self.average_pct is None or self.previous_week_average_pct is None:
            return None
        return self.average_pct - self.previous_week_average_pct

    def as_dict(self) -> dict[str, Any]:
        return {
            "average_pct": self.average_pct,
            "max_pct": self.max_pct,
            "max_at": self.max_at.isoformat() if self.max_at else None,
            "minutes_above": self.minutes_above,
            "minutes_counted": self.minutes_counted,
            "minutes_missing": self.minutes_missing,
            "percent_time_above": self.percent_time_above,
            "previous_week_average_pct": self.previous_week_average_pct,
            "week_over_week_delta_pct": self.week_over_week_delta_pct,
            "threshold_pct": self.threshold_pct,
            "above_threshold": self.above_threshold,
        }


def summarise(series: ChannelSeries, thresholds: Thresholds) -> Stats:
    """The figures for one channel, from its current-week rows.

    A channel qualifies as a rebuffering channel on its **average** alone. One minute spiking
    above the threshold does not qualify it, which is what separates a channel with a bad
    week from a channel with one bad minute.
    """
    measured = [p for p in series.origin if p.value is not None]
    average = _average(series.origin)
    peak = max(measured, key=lambda p: p.value or 0.0) if measured else None
    above = sum(1 for p in measured if is_above(p.value, thresholds))

    return Stats(
        average_pct=average,
        max_pct=peak.value if peak else None,
        max_at=peak.at if peak else None,
        minutes_above=above,
        minutes_counted=len(measured),
        minutes_missing=len(series.origin) - len(measured),
        percent_time_above=(above / len(measured) * 100) if measured else None,
        previous_week_average_pct=_average(series.comparison),
        threshold_pct=thresholds.cascada_rebuffering_threshold_pct,
        above_threshold=is_above(average, thresholds),
    )
