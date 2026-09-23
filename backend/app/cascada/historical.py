"""CASCADA historical rebuffering: one value per UTC day, for the last seven complete days.

The realtime source reads per-minute rows for a rolling window that ends at the current
minute. This one POSTs to `/api/data/v1/historical` with `period_type=Day` and reads one row
per channel per day. Everything downstream — the threshold, the red row, the country report,
the Bulk hand-off, a batch — is the realtime pipeline's, fed a `ChannelWindow` whose `origin`
holds days instead of minutes.

**The window.** "Last 7 days" is the seven most recent complete UTC days, today excluded: on
2026-09-23 that is 2026-09-16 → 2026-09-22. In the sample CASCADA answered, `from` = 2026-09-15
00:00 and `to` = 2026-09-22 00:00 returned eight rows, 20260915 … 20260922 — both ends
inclusive, a day for each midnight named — and the row for the day of the request was all
null because that day was incomplete. So `to` is 00:00 UTC yesterday and `from` is six days
before it. The dates shown anywhere are the dates CASCADA returned inside that range, and a
row for a day outside it (today, if a future answer includes it) is counted and dropped, never
averaged.

**The response is columnar.** `columns` names each position and `data` is a list of rows in
that order. Values are read by column name, because the order is CASCADA's to change. Rows
are grouped by `channel_id`, which is what lets one POST carry many channels.

**The average** is the mean of the days that carry a value. A null day is a gap, counted and
shown as "no data", never a zero. A channel with fewer than `cascada_historical_min_days`
days of data is "insufficient data": not flagged and not passed, and listed on its own. Only
`origin` rows are averaged; a row in any other category is kept for display and never mixed in.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.cascada import client
from app.cascada.auth import CSRF_COOKIE, CascadaAuth, CascadaAuthError, resolve_auth
from app.cascada.client import CascadaError, _auth_failure
from app.cascada.series import Point, Stats, Window, is_above
from app.cascada.store import ChannelWindow
from app.config import Thresholds, get_settings, get_thresholds
from app.db import session as db_session
from app.db.models import CascadaDailySample
from app.net.fetcher import Fetcher

logger = logging.getLogger(__name__)

SOURCE = "historical"
API_PATH = "/api/data/v1/historical"
DAYS = 7
ORIGIN = "origin"
METRIC = "rebuffering_ratio"

# Every field of the request except `from`, `to` and `channel_name_list`.
REQUEST_CONSTANTS: dict[str, Any] = {
    "period_type": "Day",
    "channel_group_name": client.CHANNEL_GROUP_NAME,
    "channel_country_list": [client.CHANNEL_COUNTRY],
    "model_list": [{"platform": client.PLATFORM, "model": client.MODEL}],
    "data_type": "basic",
    "target_metrics": [METRIC],
    "response_type": "minimize",
    "is_cache_key": True,
}


# -- the window --------------------------------------------------------------


def _midnight(day: dt.date) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC)


@dataclass(frozen=True, slots=True)
class DayWindow:
    """A run of whole UTC days, both ends included."""

    first: dt.date
    last: dt.date

    @property
    def days(self) -> list[dt.date]:
        span = (self.last - self.first).days
        return [self.first + dt.timedelta(days=n) for n in range(span + 1)]

    def epochs(self) -> tuple[int, int]:
        """`from` and `to` as CASCADA reads them: each day's 00:00 UTC, both days included."""
        return int(_midnight(self.first).timestamp()), int(_midnight(self.last).timestamp())

    def as_window(self) -> Window:
        return Window(start=_midnight(self.first), end=_midnight(self.last))

    def label(self) -> str:
        return f"{self.first.isoformat()} → {self.last.isoformat()}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "first_day": self.first.isoformat(),
            "last_day": self.last.isoformat(),
            "days": len(self.days),
            "label": self.label(),
        }


def window_for(now: dt.datetime | None = None, days: int = DAYS) -> DayWindow:
    """The `days` most recent complete UTC days, today excluded."""
    moment = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    last = moment.date() - dt.timedelta(days=1)
    return DayWindow(first=last - dt.timedelta(days=max(1, days) - 1), last=last)


def window_of(window: Window) -> DayWindow:
    """A stored window read back as days."""
    return DayWindow(first=window.start.astimezone(dt.UTC).date(), last=window.end.date())


# -- the request -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoricalChannel:
    """One catalogue channel, with what the provider map says CASCADA calls it."""

    service_id: str
    provider_name: str
    # CASCADA's name for this channel id, which is what the request carries.
    cascada_name: str
    # The catalogue's name, which the product shows.
    catalogue_name: str
    country: str = ""

    def entry(self) -> dict[str, str]:
        return {
            "provider_name": self.provider_name,
            "channel_name": self.cascada_name,
            "channel_id": self.service_id,
        }


def payload(window: DayWindow, channels: list[HistoricalChannel]) -> dict[str, Any]:
    start, end = window.epochs()
    return {
        "from": start,
        "to": end,
        **REQUEST_CONSTANTS,
        "channel_name_list": [channel.entry() for channel in channels],
    }


class CsrfTokenMissing(CascadaAuthError):
    """The session has no `csrftoken`, which a POST needs and a GET does not.

    It is the session's shape, not its validity: the realtime source works with it, so this
    never marks the stored session invalid.
    """


def post_headers(auth: CascadaAuth, base_url: str | None = None) -> dict[str, str]:
    """The session headers plus what Django's CSRF check asks of a POST.

    A cookie-authenticated GET needs only the cookie. A POST over HTTPS also needs the
    `X-CSRFToken` header matching the `csrftoken` cookie — `auth.headers()` sends it when the
    pasted session included one — and a `Referer` on the same origin. The GET headers the
    realtime call uses are not changed; these are built here, for this call only.
    """
    headers = dict(auth.headers())
    if "X-CSRFToken" not in headers:
        raise CsrfTokenMissing(
            "Historical data is requested with a POST, and CASCADA checks a POST against the "
            f"session's `{CSRF_COOKIE}` cookie. The configured session has none: paste the whole "
            "Cookie header from a signed-in CASCADA tab in Settings → CASCADA."
        )
    origin = (base_url or get_settings().cascada_base_url).rstrip("/")
    headers["Content-Type"] = "application/json"
    headers["Origin"] = origin
    headers["Referer"] = f"{origin}/"
    return headers


# -- the response ------------------------------------------------------------


class HistoricalParseError(CascadaError):
    """The response does not carry the columns a historical answer has."""


def _day(raw: Any) -> dt.date | None:
    """`20260915` (what CASCADA writes) or an ISO date or timestamp."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if len(text) == 8 and text.isdigit():
            return dt.date(int(text[:4]), int(text[4:6]), int(text[6:]))
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _value(raw: Any) -> float | None:
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
class DaySeries:
    """One channel's rows from a historical response."""

    channel_id: str
    # Every day of the window, in order; a day CASCADA returned nothing for is None.
    days: dict[dt.date, float | None]
    # Days CASCADA returned a row for inside the window, whatever the value.
    returned: list[dt.date] = field(default_factory=list)
    # Rows in a category other than `origin`, kept for display only.
    other: list[dict[str, Any]] = field(default_factory=list)
    # `origin` rows dated outside the window, which are dropped.
    outside: int = 0
    provider_name: str = ""
    channel_name: str = ""

    @property
    def days_with_data(self) -> int:
        return sum(1 for value in self.days.values() if value is not None)


def parse(body: Any, window: DayWindow) -> dict[str, DaySeries]:
    """Every channel's days from one response, keyed by `channel_id`."""
    if not isinstance(body, dict):
        raise HistoricalParseError(
            f"CASCADA answered the historical call with a {type(body).__name__}, not an object."
        )
    columns = body.get("columns")
    rows = body.get("data")
    if not isinstance(columns, list) or not isinstance(rows, list):
        keys = ", ".join(sorted(str(k) for k in body)) or "none"
        raise HistoricalParseError(
            f"The historical response carries no `columns`/`data` table. Keys present: {keys}."
        )
    index = {str(name): position for position, name in enumerate(columns)}
    needed = ("target_time", "channel_id", METRIC)
    missing = [name for name in needed if name not in index]
    if missing:
        raise HistoricalParseError(
            f"The historical response has no {', '.join(missing)} column. "
            f"Columns present: {', '.join(str(c) for c in columns)}."
        )

    def cell(row: list[Any], name: str) -> Any:
        position = index.get(name)
        return row[position] if position is not None and position < len(row) else None

    wanted = set(window.days)
    found: dict[str, DaySeries] = {}
    for row in rows:
        if not isinstance(row, list):
            continue
        channel_id = str(cell(row, "channel_id") or "").strip()
        day = _day(cell(row, "target_time"))
        if not channel_id or day is None:
            continue
        series = found.setdefault(
            channel_id,
            DaySeries(channel_id=channel_id, days=dict.fromkeys(window.days)),
        )
        # A null provider or name on an empty day says nothing about the channel.
        series.provider_name = series.provider_name or str(cell(row, "provider_name") or "")
        series.channel_name = series.channel_name or str(cell(row, "channel_name") or "")
        value = _value(cell(row, METRIC))
        category = str(cell(row, "date_category") or ORIGIN).strip()

        if category != ORIGIN:
            series.other.append({"day": day.isoformat(), "category": category, "value": value})
            continue
        if day not in wanted:
            series.outside += 1
            continue
        series.days[day] = value
        series.returned.append(day)

    for series in found.values():
        series.returned.sort()
    return found


# -- the figures -------------------------------------------------------------


@dataclass(slots=True)
class DayStats:
    average_pct: float | None
    max_pct: float | None
    max_day: dt.date | None
    days_above: int
    days_with_data: int
    days_expected: int
    min_days: int
    threshold_pct: float
    insufficient: bool
    above_threshold: bool


def summarise(series: DaySeries, thresholds: Thresholds) -> DayStats:
    """The figures for one channel, from its `origin` days.

    The threshold comparison is `series.is_above`, the one the realtime source uses. A
    channel short of the minimum days is never above — nor is it passed: `insufficient` says
    which it is, and the scan lists it separately.
    """
    measured = [(day, value) for day, value in series.days.items() if value is not None]
    values = [value for _, value in measured]
    average = sum(values) / len(values) if values else None
    peak = max(measured, key=lambda pair: pair[1]) if measured else None
    min_days = max(1, thresholds.cascada_historical_min_days)
    insufficient = len(measured) < min_days
    return DayStats(
        average_pct=average,
        max_pct=peak[1] if peak else None,
        max_day=peak[0] if peak else None,
        days_above=sum(1 for value in values if is_above(value, thresholds)),
        days_with_data=len(measured),
        days_expected=len(series.days),
        min_days=min_days,
        threshold_pct=thresholds.cascada_rebuffering_threshold_pct,
        insufficient=insufficient,
        above_threshold=(not insufficient) and is_above(average, thresholds),
    )


def _returned_window(series: DaySeries, window: DayWindow) -> DayWindow:
    """The dates CASCADA returned for this channel, or the requested ones when it returned none."""
    if not series.returned:
        return window
    return DayWindow(first=series.returned[0], last=series.returned[-1])


def to_window(
    *,
    series: DaySeries,
    window: DayWindow,
    channel: HistoricalChannel,
    thresholds: Thresholds,
    fetched_at: dt.datetime | None = None,
    cached: bool = False,
) -> ChannelWindow:
    """One channel's days as the shape the scan, the reports and the modal read.

    `Stats.minutes_above` and `minutes_counted` carry days here; `granularity="day"` is what
    says so, and every surface that prints them reads it.
    """
    stats = summarise(series, thresholds)
    returned = _returned_window(series, window)
    return ChannelWindow(
        service_id=channel.service_id,
        channel_name=channel.catalogue_name,
        country=channel.country,
        window=window.as_window(),
        stats=Stats(
            average_pct=stats.average_pct,
            max_pct=stats.max_pct,
            max_at=_midnight(stats.max_day) if stats.max_day else None,
            minutes_above=stats.days_above,
            minutes_counted=stats.days_with_data,
            minutes_missing=stats.days_expected - stats.days_with_data,
            percent_time_above=(
                stats.days_above / stats.days_with_data * 100 if stats.days_with_data else None
            ),
            previous_week_average_pct=None,
            threshold_pct=stats.threshold_pct,
            above_threshold=stats.above_threshold,
        ),
        origin=[Point(at=_midnight(day), value=value) for day, value in series.days.items()],
        comparison=[],
        fetched_at=fetched_at or dt.datetime.now(dt.UTC),
        truncated=False,
        cached=cached,
        source=SOURCE,
        granularity="day",
        details={
            "days_with_data": stats.days_with_data,
            "days_expected": stats.days_expected,
            "days_above": stats.days_above,
            "min_days": stats.min_days,
            "insufficient": stats.insufficient,
            "requested_days": window.as_dict(),
            "returned_days": returned.as_dict(),
            "returned_dates": [day.isoformat() for day in series.returned],
            "provider_name": channel.provider_name,
            "cascada_channel_name": channel.cascada_name,
            "other_categories": series.other,
            "rows_outside_window": series.outside,
        },
    )


# -- the call ----------------------------------------------------------------


def _url() -> str:
    return f"{get_settings().cascada_base_url.rstrip('/')}{API_PATH}"


def _parse_body(text: str, window: DayWindow) -> dict[str, DaySeries]:
    try:
        # CASCADA writes a few channel names with raw newlines in them.
        body = json.loads(text, strict=False)
    except ValueError as exc:
        raise CascadaError(
            f"CASCADA returned {len(text)} byte(s) for the historical call that do not parse "
            f"as JSON: {exc}."
        ) from exc
    return parse(body, window)


async def post_days(
    channels: list[HistoricalChannel],
    window: DayWindow,
    auth: CascadaAuth,
    fetcher: Fetcher,
) -> dict[str, DaySeries]:
    """One POST for these channels, with the realtime call's retry and backoff."""
    headers = post_headers(auth)
    body = json.dumps(payload(window, channels)).encode("utf-8")
    last_error = ""
    for attempt in range(1, client.RETRY_ATTEMPTS + 1):
        result = await fetcher.fetch(_url(), method="POST", headers=headers, content=body)
        reason = _auth_failure(result.status, result.final_url)
        if reason is not None:
            raise CascadaAuthError(
                f"{reason} The historical call is a POST, which CASCADA also checks against "
                f"the session's `{CSRF_COOKIE}`. Paste a fresh session in the Settings tab."
            )
        if result.ok:
            return _parse_body(result.text, window)
        last_error = (
            f"the call failed on the wire ({result.error})"
            if result.error is not None
            else f"CASCADA answered HTTP {result.status}"
        )
        if result.error is None and 400 <= result.status < 500:
            break
        if attempt < client.RETRY_ATTEMPTS:
            await asyncio.sleep(client.RETRY_BACKOFF_S * attempt)
    names = ", ".join(channel.service_id for channel in channels[:5])
    more = f" and {len(channels) - 5} more" if len(channels) > 5 else ""
    raise CascadaError(f"CASCADA did not serve historical data for {names}{more}: {last_error}.")


async def fetch_days(
    channels: list[HistoricalChannel],
    window: DayWindow,
    auth: CascadaAuth,
    fetcher: Fetcher,
) -> tuple[dict[str, DaySeries], dict[str, str]]:
    """Days for every channel given, and the reason for each one that has none.

    The channels go in one POST. When that call fails, or answers without a channel that was
    in it, each missing channel is asked for on its own — one bad channel must not cost the
    other twenty-four theirs. A rejected session is raised, because every call would fail.
    """
    results: dict[str, DaySeries] = {}
    failures: dict[str, str] = {}
    batch_error = ""
    try:
        results.update(await post_days(channels, window, auth, fetcher))
    except CascadaAuthError:
        raise
    except CascadaError as exc:
        batch_error = str(exc)

    missing = [channel for channel in channels if channel.service_id not in results]
    if not missing:
        return results, failures
    if len(channels) == 1:
        failures[channels[0].service_id] = (
            batch_error or "CASCADA answered the historical call with no row for this channel."
        )
        return results, failures

    if batch_error:
        logger.warning(
            "historical call for %d channel(s) failed (%s); retrying each on its own",
            len(channels),
            batch_error,
        )
    for channel in missing:
        try:
            single = await post_days([channel], window, auth, fetcher)
        except CascadaAuthError:
            raise
        except CascadaError as exc:
            failures[channel.service_id] = str(exc)
            continue
        if channel.service_id in single:
            results[channel.service_id] = single[channel.service_id]
        else:
            failures[channel.service_id] = (
                "CASCADA answered the historical call with no row for this channel "
                f"(provider {channel.provider_name}, name {channel.cascada_name!r})."
            )
    return results, failures


# -- the store ---------------------------------------------------------------


def _matching(service_id: str, window: DayWindow) -> Any:
    start, end = window.epochs()
    return select(CascadaDailySample).where(
        CascadaDailySample.service_id == service_id,
        CascadaDailySample.window_from == start,
        CascadaDailySample.window_to == end,
    )


def _from_row(row: CascadaDailySample, thresholds: Thresholds) -> ChannelWindow:
    """A stored window, re-judged against today's threshold and minimum days."""
    stored = row.series if isinstance(row.series, dict) else {}
    window = DayWindow(
        first=dt.datetime.fromtimestamp(row.window_from, dt.UTC).date(),
        last=dt.datetime.fromtimestamp(row.window_to, dt.UTC).date(),
    )
    days: dict[dt.date, float | None] = dict.fromkeys(window.days)
    for item in stored.get("days") or []:
        day = _day(item.get("day")) if isinstance(item, dict) else None
        if day in days and isinstance(item, dict):
            days[day] = _value(item.get("value"))
    series = DaySeries(
        channel_id=row.service_id,
        days=days,
        returned=[d for d in (_day(x) for x in stored.get("returned") or []) if d is not None],
        other=list(stored.get("other") or []),
        outside=int(stored.get("outside") or 0),
    )
    channel = HistoricalChannel(
        service_id=row.service_id,
        provider_name=str(stored.get("provider_name") or ""),
        cascada_name=str(stored.get("cascada_channel_name") or ""),
        catalogue_name=row.channel_name,
        country=row.country or "",
    )
    fetched = row.fetched_at if row.fetched_at.tzinfo else row.fetched_at.replace(tzinfo=dt.UTC)
    return to_window(
        series=series,
        window=window,
        channel=channel,
        thresholds=thresholds,
        fetched_at=fetched,
        cached=True,
    )


def _fresh(entry: ChannelWindow, thresholds: Thresholds) -> bool:
    return entry.age_minutes <= thresholds.cascada_cache_ttl_minutes


async def read(service_id: str, window: DayWindow, thresholds: Thresholds) -> ChannelWindow | None:
    try:
        async with db_session.session_scope() as session:
            row = (await session.execute(_matching(service_id, window))).scalars().first()
            if row is None:
                return None
            entry = _from_row(row, thresholds)
    except Exception as exc:
        logger.warning("the CASCADA historical store could not be read: %s", type(exc).__name__)
        return None
    return entry if _fresh(entry, thresholds) else None


async def read_country(
    country: str, window: DayWindow, thresholds: Thresholds
) -> dict[str, ChannelWindow]:
    start, end = window.epochs()
    try:
        async with db_session.session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(CascadaDailySample).where(
                            CascadaDailySample.country == country,
                            CascadaDailySample.window_from == start,
                            CascadaDailySample.window_to == end,
                        )
                    )
                )
                .scalars()
                .all()
            )
            entries = [_from_row(row, thresholds) for row in rows]
    except Exception as exc:
        logger.warning("the CASCADA historical store could not be read: %s", type(exc).__name__)
        return {}
    return {entry.service_id: entry for entry in entries if _fresh(entry, thresholds)}


async def write(entry: ChannelWindow) -> None:
    """Store one channel's days, replacing whatever was held for the same window."""
    details = entry.details
    window = window_of(entry.window)
    start, end = window.epochs()
    payload_json = {
        "days": [
            {"day": point.at.date().isoformat(), "value": point.value} for point in entry.origin
        ],
        "returned": details.get("returned_dates") or [],
        "other": details.get("other_categories") or [],
        "outside": details.get("rows_outside_window") or 0,
        "provider_name": details.get("provider_name") or "",
        "cascada_channel_name": details.get("cascada_channel_name") or "",
    }
    try:
        async with db_session.session_scope() as session:
            found = (await session.execute(_matching(entry.service_id, window))).scalars().first()
            row = found or CascadaDailySample(
                service_id=entry.service_id, window_from=start, window_to=end
            )
            row.channel_name = entry.channel_name
            row.country = entry.country or None
            row.fetched_at = entry.fetched_at
            row.average_pct = entry.stats.average_pct
            row.days_with_data = int(details.get("days_with_data") or 0)
            row.series = payload_json
            if found is None:
                session.add(row)
    except Exception as exc:
        # Failing to cache must never fail the call that produced the measurement.
        logger.warning(
            "the CASCADA historical window for %s was not stored: %s",
            entry.service_id,
            type(exc).__name__,
        )


# -- one channel -------------------------------------------------------------


class ProviderNotFound(CascadaError):
    """The channel-group list has no on-service provider for this channel id."""


async def resolve_channel(
    *, service_id: str, channel_name: str, country: str, auth: CascadaAuth
) -> tuple[HistoricalChannel, dict[str, Any]]:
    """The channel as the historical call names it, and what the provider map said."""
    from app.cascada import providers

    mapping = await providers.provider_map(auth)
    resolution = mapping.resolve(service_id)
    if resolution.chosen is None:
        raise ProviderNotFound(
            f"Provider not found: CASCADA's channel list has no on-service entry for "
            f"{service_id} in '{mapping.group}', so its historical data cannot be requested."
        )
    if resolution.status == "ambiguous":
        logger.warning(
            "provider for %s is ambiguous; using %s of %s",
            service_id,
            resolution.chosen.provider_name,
            [c.provider_name for c in resolution.candidates],
        )
    if resolution.chosen.channel_name and resolution.chosen.channel_name != channel_name:
        logger.info(
            "CASCADA names %s %r where the catalogue says %r; the historical call uses CASCADA's",
            service_id,
            resolution.chosen.channel_name,
            channel_name,
        )
    return (
        HistoricalChannel(
            service_id=service_id,
            provider_name=resolution.chosen.provider_name,
            cascada_name=resolution.chosen.channel_name or channel_name,
            catalogue_name=channel_name,
            country=country,
        ),
        resolution.as_dict(),
    )


async def channel_days(
    *,
    service_id: str,
    channel_name: str,
    country: str,
    thresholds: Thresholds | None = None,
    window: DayWindow | None = None,
    auth: CascadaAuth | None = None,
    refresh: bool = False,
) -> ChannelWindow:
    """One channel's last seven complete days, from the store when fresh, else from CASCADA."""
    limits = thresholds or get_thresholds()
    span = window or window_for()
    if not refresh:
        held = await read(service_id, span, limits)
        if held is not None:
            return held

    provider = auth or await resolve_auth()
    channel, resolution = await resolve_channel(
        service_id=service_id, channel_name=channel_name, country=country, auth=provider
    )
    settings = get_settings()
    fetcher = Fetcher(
        timeout_s=settings.cascada_timeout_s,
        per_host_connections=settings.rba_per_host_connections,
    )
    try:
        results, failures = await fetch_days([channel], span, provider, fetcher)
    finally:
        await fetcher.aclose()
    if service_id not in results:
        raise CascadaError(failures.get(service_id) or "CASCADA returned no historical row.")
    entry = to_window(series=results[service_id], window=span, channel=channel, thresholds=limits)
    entry.details["provider_resolution"] = resolution
    await write(entry)
    return entry
