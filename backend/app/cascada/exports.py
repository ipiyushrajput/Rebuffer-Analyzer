"""CSV and XLSX for the CASCADA reports.

Three reports: one country's rebuffering channels, one channel's minute-by-minute rebuffering,
and one channel's minute-by-minute error count.

Both are written as a table, not as a form. The column names sit on the first row and each
record runs left to right beneath them, which is what a spreadsheet sorts, filters and pivots.
An earlier version opened with the window and the threshold stacked down column A, so the
first thing anyone saw was a block that read top to bottom and the real header row began
halfway down the sheet. That block is still written — an average means nothing without the
window it covers — but it goes after the data in a CSV and onto its own sheet in a workbook,
where it explains the table without getting in the way of it.

Every rebuffering figure is a percentage of viewing time, and every error figure a count of
errors per minute; both are taken from the current week's rows alone.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from app.cascada.client import CHANNEL_COUNTRY, describe_window
from app.cascada.errors import ErrorWindow
from app.cascada.series import Window
from app.cascada.store import ChannelWindow

CHANNEL_COLUMNS = (
    "timestamp_utc",
    "rebuffering_ratio_pct",
    "above_threshold",
)

COUNTRY_COLUMNS = (
    "channel_name",
    "channel_id",
    "country",
    "playback_url",
    "avg_rebuffering_ratio_pct",
    "max_rebuffering_ratio_pct",
    "max_at_utc",
    "minutes_above_threshold",
    "minutes_measured",
    "percent_time_above_threshold",
    "previous_week_avg_pct",
)

# A historical scan measures days, so the two count columns say days. The order is the same,
# so a spreadsheet built on one report reads the other.
HISTORICAL_COUNTRY_COLUMNS = (
    "channel_name",
    "channel_id",
    "country",
    "playback_url",
    "avg_rebuffering_ratio_pct",
    "max_rebuffering_ratio_pct",
    "max_day_utc",
    "days_above_threshold",
    "days_with_data",
    "percent_days_above_threshold",
    "previous_week_avg_pct",
)

# Channels a historical scan could not judge, listed after the table and never dropped.
NOT_JUDGED_COLUMNS = ("channel_name", "channel_id", "status", "reason")
NOT_JUDGED_SHEET = "not_judged"

HISTORICAL_CHANNEL_COLUMNS = ("day_utc", "rebuffering_ratio_pct", "above_threshold")

ABOUT_SHEET = "about"


def _pct(value: float | None) -> str:
    """A percentage as the report prints it, or an empty cell for a minute never measured."""
    return "" if value is None else f"{value:.4f}"


def _scope_note() -> str:
    """What the figures cover, so a country report is not read as a per-country measurement."""
    return (
        f"CASCADA is queried with channel_country={CHANNEL_COUNTRY}, so each figure is the "
        "channel's rebuffering across every country it runs in, not its rebuffering in the "
        "country selected here."
    )


# -- the country report ------------------------------------------------------


def _country_row(entry: ChannelWindow, playback_url: str) -> list[Any]:
    stats = entry.stats
    return [
        entry.channel_name,
        entry.service_id,
        entry.country,
        playback_url,
        _pct(stats.average_pct),
        _pct(stats.max_pct),
        _max_at(entry),
        stats.minutes_above,
        stats.minutes_counted,
        _pct(stats.percent_time_above),
        _pct(stats.previous_week_average_pct),
    ]


def _max_at(entry: ChannelWindow) -> str:
    """When the maximum was: a minute for realtime, a day for historical."""
    moment = entry.stats.max_at
    if moment is None:
        return ""
    return moment.date().isoformat() if entry.granularity == "day" else moment.isoformat()


def _country_about(
    *,
    country: str,
    window: Window,
    threshold_pct: float,
    rows: int,
    partial: bool,
    scanned: str,
    source: str = "realtime",
    window_label: str = "",
    min_days: int | None = None,
) -> list[list[str]]:
    """What the table above was measured against, as label/value pairs."""
    about = [
        ["field", "value"],
        ["report", "Samsung TV Plus — CASCADA rebuffering report"],
        ["country", country],
        ["source", _source_note(source)],
        ["window_utc", window_label if source == "historical" else describe_window(window)],
        ["threshold_pct", f"{threshold_pct:.4f}"],
        ["selection", "Channels whose average over the window is above the threshold"],
        ["channels_listed", str(rows)],
        ["scan", scanned],
        ["scope", _scope_note()],
    ]
    if source == "historical" and min_days is not None:
        about.append(
            [
                "minimum_days_with_data",
                f"{min_days} of 7; a channel with fewer is listed as insufficient data",
            ]
        )
    if partial:
        about.append(
            [
                "note",
                "The scan had not finished when this was downloaded, so channels it has not "
                "measured yet are absent.",
            ]
        )
    return about


def _source_note(source: str) -> str:
    if source == "historical":
        return "historical — one value per UTC day, the last 7 complete days"
    return "realtime — one value per minute"


def _columns(source: str) -> tuple[str, ...]:
    return HISTORICAL_COUNTRY_COLUMNS if source == "historical" else COUNTRY_COLUMNS


def country_csv(
    entries: list[ChannelWindow],
    *,
    country: str,
    window: Window,
    threshold_pct: float,
    partial: bool,
    scanned: str,
    urls: dict[str, str] | None = None,
    source: str = "realtime",
    window_label: str = "",
    not_judged: list[list[str]] | None = None,
    min_days: int | None = None,
) -> str:
    """One country's rebuffering channels, worst first, as a table with its header on row 1."""
    playback = urls or {}
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(_columns(source)))
    for entry in entries:
        writer.writerow(_country_row(entry, playback.get(entry.service_id, "")))

    if not_judged:
        writer.writerow([])
        writer.writerow(["Channels not judged"])
        writer.writerow(list(NOT_JUDGED_COLUMNS))
        writer.writerows(not_judged)

    # The context follows the data, so row 1 is still the header a spreadsheet reads.
    writer.writerow([])
    writer.writerows(
        _country_about(
            country=country,
            window=window,
            threshold_pct=threshold_pct,
            rows=len(entries),
            partial=partial,
            scanned=scanned,
            source=source,
            window_label=window_label,
            min_days=min_days,
        )
    )
    return buffer.getvalue()


def country_xlsx(
    entries: list[ChannelWindow],
    *,
    country: str,
    window: Window,
    threshold_pct: float,
    partial: bool,
    scanned: str,
    urls: dict[str, str] | None = None,
    source: str = "realtime",
    window_label: str = "",
    not_judged: list[list[str]] | None = None,
    min_days: int | None = None,
) -> bytes:
    from openpyxl import Workbook

    playback = urls or {}
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "rebuffering"
    sheet.append(list(_columns(source)))
    for entry in entries:
        sheet.append(_country_row(entry, playback.get(entry.service_id, "")))
    # The header stays visible while a long country is scrolled.
    sheet.freeze_panes = "A2"

    if not_judged:
        skipped = workbook.create_sheet(NOT_JUDGED_SHEET)
        skipped.append(list(NOT_JUDGED_COLUMNS))
        for line in not_judged:
            skipped.append(line)
        skipped.freeze_panes = "A2"

    about = workbook.create_sheet(ABOUT_SHEET)
    for line in _country_about(
        country=country,
        window=window,
        threshold_pct=threshold_pct,
        rows=len(entries),
        partial=partial,
        scanned=scanned,
        source=source,
        window_label=window_label,
        min_days=min_days,
    ):
        about.append(line)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# -- one channel -------------------------------------------------------------


def _channel_row(entry: ChannelWindow, point: Any) -> list[Any]:
    above = (
        "" if point.value is None else ("yes" if point.value > entry.stats.threshold_pct else "no")
    )
    return [point.at.isoformat(), _pct(point.value), above]


def _channel_about(entry: ChannelWindow) -> list[list[str]]:
    stats = entry.stats
    about = [
        ["field", "value"],
        ["report", "Samsung TV Plus — CASCADA rebuffering report"],
        ["channel", entry.channel_name],
        ["service_id", entry.service_id],
        ["country", entry.country],
        ["window_utc", describe_window(entry.window)],
        ["threshold_pct", f"{stats.threshold_pct:.4f}"],
        ["average_pct", _pct(stats.average_pct)],
        ["maximum_pct", _pct(stats.max_pct)],
        ["maximum_at_utc", stats.max_at.isoformat() if stats.max_at else ""],
        ["minutes_above_threshold", str(stats.minutes_above)],
        ["minutes_measured", str(stats.minutes_counted)],
        ["minutes_with_no_measurement", str(stats.minutes_missing)],
        ["previous_week_avg_pct", _pct(stats.previous_week_average_pct)],
        ["measured_at_utc", entry.fetched_at.isoformat()],
        ["scope", _scope_note()],
    ]
    if entry.truncated:
        about.append(
            [
                "note",
                "CASCADA returned fewer minutes than the window asked for, so these figures "
                "cover less than the window above.",
            ]
        )
    return about


def channel_csv(entry: ChannelWindow) -> str:
    """One channel: one row per measured minute, then what those minutes were measured against."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(CHANNEL_COLUMNS))
    for point in entry.origin:
        writer.writerow(_channel_row(entry, point))

    writer.writerow([])
    writer.writerows(_channel_about(entry))
    return buffer.getvalue()


def channel_xlsx(entry: ChannelWindow) -> bytes:
    """The same content as a workbook: the minutes on one sheet, the context on another."""
    from openpyxl import Workbook

    workbook = Workbook()
    minutes = workbook.active
    minutes.title = "minutes"
    minutes.append(list(CHANNEL_COLUMNS))
    for point in entry.origin:
        minutes.append([point.at.isoformat(), point.value, _channel_row(entry, point)[2] or None])
    minutes.freeze_panes = "A2"

    about = workbook.create_sheet(ABOUT_SHEET)
    for line in _channel_about(entry):
        about.append(line)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# -- one channel's historical days -------------------------------------------


def _day_row(entry: ChannelWindow, point: Any) -> list[Any]:
    above = (
        "" if point.value is None else ("yes" if point.value > entry.stats.threshold_pct else "no")
    )
    return [point.at.date().isoformat(), _pct(point.value), above]


def _historical_about(entry: ChannelWindow) -> list[list[str]]:
    stats, details = entry.stats, entry.details
    returned = details.get("returned_days") or {}
    about = [
        ["field", "value"],
        ["report", "Samsung TV Plus — CASCADA historical rebuffering report"],
        ["source", _source_note("historical")],
        ["channel", entry.channel_name],
        ["service_id", entry.service_id],
        ["country", entry.country],
        ["cascada_channel_name", str(details.get("cascada_channel_name", ""))],
        ["provider_name", str(details.get("provider_name", ""))],
        ["window_utc", str(returned.get("label", ""))],
        ["threshold_pct", f"{stats.threshold_pct:.4f}"],
        ["average_pct", _pct(stats.average_pct)],
        ["maximum_pct", _pct(stats.max_pct)],
        ["maximum_day_utc", _max_at(entry)],
        ["days_above_threshold", str(stats.minutes_above)],
        [
            "days_with_data",
            f"{details.get('days_with_data', 0)} of {details.get('days_expected', 7)}",
        ],
        [
            "minimum_days_with_data",
            f"{details.get('min_days', '')}"
            + (
                "; below it, so this channel is insufficient data"
                if details.get("insufficient")
                else ""
            ),
        ],
        ["measured_at_utc", entry.fetched_at.isoformat()],
        ["scope", _scope_note()],
    ]
    return about


def historical_csv(entry: ChannelWindow) -> str:
    """One channel: one row per day, a day with no data left empty, then the context."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(HISTORICAL_CHANNEL_COLUMNS))
    for point in entry.origin:
        writer.writerow(_day_row(entry, point))
    writer.writerow([])
    writer.writerows(_historical_about(entry))
    return buffer.getvalue()


def historical_xlsx(entry: ChannelWindow) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    days = workbook.active
    days.title = "days"
    days.append(list(HISTORICAL_CHANNEL_COLUMNS))
    for point in entry.origin:
        days.append([point.at.date().isoformat(), point.value, _day_row(entry, point)[2] or None])
    days.freeze_panes = "A2"
    about = workbook.create_sheet(ABOUT_SHEET)
    for line in _historical_about(entry):
        about.append(line)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# -- one channel's errors ----------------------------------------------------

ERROR_COLUMNS = (
    "timestamp_utc",
    "errors",
    "above_threshold",
)


def _count(value: float | None) -> str:
    """An error count as the report prints it, or an empty cell for a minute never measured."""
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def _error_row(entry: ErrorWindow, point: Any) -> list[Any]:
    threshold = entry.stats.threshold_per_min
    above = (
        ""
        if point.value is None or threshold is None
        else ("yes" if point.value > threshold else "no")
    )
    return [point.at.isoformat(), _count(point.value), above]


def _error_about(entry: ErrorWindow) -> list[list[str]]:
    stats = entry.stats
    threshold = stats.threshold_per_min
    about = [
        ["field", "value"],
        ["report", "Samsung TV Plus — CASCADA error report"],
        ["channel", entry.channel_name],
        ["service_id", entry.service_id],
        ["country", entry.country],
        ["window_utc", describe_window(entry.window)],
        ["unit", f"errors per minute ({entry.unit})"],
        [
            "threshold_per_min",
            _count(threshold) if threshold is not None else "not set (0 in Settings)",
        ],
        ["average_per_min", _count(stats.average_per_min)],
        ["maximum_per_min", _count(stats.max_per_min)],
        ["maximum_at_utc", stats.max_at.isoformat() if stats.max_at else ""],
        ["total_errors", _count(stats.total)],
        ["minutes_above_threshold", str(stats.minutes_above) if threshold is not None else ""],
        ["minutes_measured", str(stats.minutes_counted)],
        ["minutes_with_no_measurement", str(stats.minutes_missing)],
        ["previous_week_avg_per_min", _count(stats.previous_week_average_per_min)],
        ["measured_at_utc", entry.fetched_at.isoformat()],
        [
            "scope",
            f"CASCADA is queried with channel_country={CHANNEL_COUNTRY}, so each figure is the "
            "channel's errors across every country it runs in.",
        ],
    ]
    if entry.truncated:
        about.append(
            [
                "note",
                "CASCADA returned fewer minutes than the window asked for, so these figures "
                "cover less than the window above.",
            ]
        )
    return about


def errors_csv(entry: ErrorWindow) -> str:
    """One channel's errors: one row per measured minute, then what they were measured against."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(ERROR_COLUMNS))
    for point in entry.origin:
        writer.writerow(_error_row(entry, point))

    writer.writerow([])
    writer.writerows(_error_about(entry))
    return buffer.getvalue()


def errors_xlsx(entry: ErrorWindow) -> bytes:
    """The same content as a workbook: the minutes on one sheet, the context on another."""
    from openpyxl import Workbook

    workbook = Workbook()
    minutes = workbook.active
    minutes.title = "minutes"
    minutes.append(list(ERROR_COLUMNS))
    for point in entry.origin:
        minutes.append([point.at.isoformat(), point.value, _error_row(entry, point)[2] or None])
    minutes.freeze_panes = "A2"

    about = workbook.create_sheet(ABOUT_SHEET)
    for line in _error_about(entry):
        about.append(line)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# -- filenames ---------------------------------------------------------------


def country_filename(
    country: str, fmt: str, now: dt.datetime | None = None, *, source: str = "realtime"
) -> str:
    stamp = (now or dt.datetime.now(dt.UTC)).strftime("%Y%m%d")
    kind = "historical_" if source == "historical" else ""
    return f"rebuffering_report_{kind}{country.upper()}_{stamp}.{fmt}"


def channel_filename(
    service_id: str, fmt: str, now: dt.datetime | None = None, *, prefix: str = "rebuffering"
) -> str:
    stamp = (now or dt.datetime.now(dt.UTC)).strftime("%Y%m%d")
    safe = "".join(ch for ch in service_id if ch.isalnum() or ch in "-_") or "channel"
    return f"{prefix}_{safe}_{stamp}.{fmt}"
