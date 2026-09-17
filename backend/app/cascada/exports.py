"""CSV and XLSX for the CASCADA rebuffering reports.

Two reports: one channel's minute-by-minute series, and one country's rebuffering channels.
Both open with a header block naming the window the figures were measured over and the
threshold they were judged against, because an average is meaningless without them and a
spreadsheet outlives the screen it was downloaded from.

Every figure is a percentage of viewing time, taken from the current-week rows alone.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from app.cascada.client import CHANNEL_COUNTRY, describe_window
from app.cascada.series import Window
from app.cascada.store import ChannelWindow

CHANNEL_COLUMNS = ("timestamp_utc", "rebuffering_ratio_pct", "above_threshold")

COUNTRY_COLUMNS = (
    "channel_name",
    "channel_id",
    "country",
    "avg_rebuffering_ratio_pct",
    "max_rebuffering_ratio_pct",
    "max_at_utc",
    "minutes_above_threshold",
    "minutes_measured",
    "percent_time_above_threshold",
    "previous_week_avg_pct",
)


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


def _channel_header(entry: ChannelWindow, *, partial: bool = False) -> list[list[str]]:
    stats = entry.stats
    header = [
        ["Samsung TV Plus — CASCADA rebuffering report"],
        ["Channel", entry.channel_name],
        ["Service ID", entry.service_id],
        ["Country", entry.country],
        ["Window (UTC)", describe_window(entry.window)],
        ["Threshold", f"{stats.threshold_pct:.4f} % of viewing time"],
        ["Average", f"{_pct(stats.average_pct)} %"],
        ["Maximum", f"{_pct(stats.max_pct)} %"],
        ["Maximum at", stats.max_at.isoformat() if stats.max_at else ""],
        ["Minutes above threshold", str(stats.minutes_above)],
        ["Minutes measured", str(stats.minutes_counted)],
        ["Minutes with no measurement", str(stats.minutes_missing)],
        ["Previous week average", f"{_pct(stats.previous_week_average_pct)} %"],
        ["Measured at", entry.fetched_at.isoformat()],
        ["Scope", _scope_note()],
    ]
    if entry.truncated or partial:
        header.append(
            [
                "Note",
                "CASCADA returned fewer minutes than the window asked for, so these figures "
                "cover less than the window above.",
            ]
        )
    header.append([])
    return header


def channel_csv(entry: ChannelWindow) -> str:
    """One channel: the summary block, then one row per measured minute."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(_channel_header(entry))
    writer.writerow(list(CHANNEL_COLUMNS))
    for point in entry.origin:
        writer.writerow(
            [
                point.at.isoformat(),
                _pct(point.value),
                (
                    ""
                    if point.value is None
                    else ("yes" if point.value > entry.stats.threshold_pct else "no")
                ),
            ]
        )
    return buffer.getvalue()


def channel_xlsx(entry: ChannelWindow) -> bytes:
    """The same content as a workbook: the summary on one sheet, the minutes on another."""
    from openpyxl import Workbook

    workbook = Workbook()
    summary = workbook.active
    summary.title = "summary"
    for line in _channel_header(entry):
        summary.append(line)

    minutes = workbook.create_sheet("minutes")
    minutes.append(list(CHANNEL_COLUMNS))
    for point in entry.origin:
        minutes.append(
            [
                point.at.isoformat(),
                point.value,
                (
                    None
                    if point.value is None
                    else ("yes" if point.value > entry.stats.threshold_pct else "no")
                ),
            ]
        )

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _country_header(
    *, country: str, window: Window, threshold_pct: float, rows: int, partial: bool, scanned: str
) -> list[list[str]]:
    header = [
        ["Samsung TV Plus — CASCADA rebuffering report"],
        ["Country", country],
        ["Window (UTC)", describe_window(window)],
        ["Threshold", f"{threshold_pct:.4f} % of viewing time"],
        ["Selection", "Channels whose average over the window is above the threshold"],
        ["Channels listed", str(rows)],
        ["Scan", scanned],
        ["Scope", _scope_note()],
    ]
    if partial:
        header.append(
            [
                "Note",
                "The scan had not finished when this was downloaded, so channels it has not "
                "measured yet are absent.",
            ]
        )
    header.append([])
    return header


def _country_row(entry: ChannelWindow) -> list[Any]:
    stats = entry.stats
    return [
        entry.channel_name,
        entry.service_id,
        entry.country,
        _pct(stats.average_pct),
        _pct(stats.max_pct),
        stats.max_at.isoformat() if stats.max_at else "",
        stats.minutes_above,
        stats.minutes_counted,
        _pct(stats.percent_time_above),
        _pct(stats.previous_week_average_pct),
    ]


def country_csv(
    entries: list[ChannelWindow],
    *,
    country: str,
    window: Window,
    threshold_pct: float,
    partial: bool,
    scanned: str,
) -> str:
    """One country's rebuffering channels, worst first."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(
        _country_header(
            country=country,
            window=window,
            threshold_pct=threshold_pct,
            rows=len(entries),
            partial=partial,
            scanned=scanned,
        )
    )
    writer.writerow(list(COUNTRY_COLUMNS))
    for entry in entries:
        writer.writerow(_country_row(entry))
    return buffer.getvalue()


def country_xlsx(
    entries: list[ChannelWindow],
    *,
    country: str,
    window: Window,
    threshold_pct: float,
    partial: bool,
    scanned: str,
) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "rebuffering"
    for line in _country_header(
        country=country,
        window=window,
        threshold_pct=threshold_pct,
        rows=len(entries),
        partial=partial,
        scanned=scanned,
    ):
        sheet.append(line)
    sheet.append(list(COUNTRY_COLUMNS))
    for entry in entries:
        sheet.append(_country_row(entry))

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def country_filename(country: str, fmt: str, now: dt.datetime | None = None) -> str:
    stamp = (now or dt.datetime.now(dt.UTC)).strftime("%Y%m%d")
    return f"rebuffering_report_{country.upper()}_{stamp}.{fmt}"


def channel_filename(service_id: str, fmt: str, now: dt.datetime | None = None) -> str:
    stamp = (now or dt.datetime.now(dt.UTC)).strftime("%Y%m%d")
    safe = "".join(ch for ch in service_id if ch.isalnum() or ch in "-_") or "channel"
    return f"rebuffering_{safe}_{stamp}.{fmt}"
