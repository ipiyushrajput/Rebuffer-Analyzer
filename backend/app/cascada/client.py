"""Talking to the CASCADA realtime API.

The request is built with `urlencode`, never by hand: the query carries a channel name with
spaces in it and a bracketed repeated parameter, and both have exactly one correct encoding.
`limit` is computed from the window the call asks for rather than fixed, because a week of
per-minute data plus its comparison week is far more rows than any constant that was written
for a shorter window.

Redirects are followed by the shared fetcher one hop at a time, which is what lets an expired
session be recognised: Django answers an unauthenticated API call by redirecting to its login
page, and a redirect to a login URL is an expired session, not a moved resource.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import Any
from urllib.parse import urlencode, urlsplit

from app.cascada.auth import CascadaAuth, CascadaAuthError
from app.cascada.series import (
    COMPARISON,
    REBUFFERING,
    CascadaParseError,
    ChannelSeries,
    Metric,
    Window,
    parse,
)
from app.config import Thresholds, get_settings
from app.net.fetcher import Fetcher

logger = logging.getLogger(__name__)

API_PATH = "/api/data/v1/realtime"

# Constants the dashboard sends and the analyzer repeats verbatim.
CHANNEL_GROUP_NAME = "Master Group - Local Channel Included"
CHANNEL_COUNTRY = "ALL"
PLATFORM = "Samsung TV"
MODEL = "ALL"
COMPARE_WITH = "1WEEK"
EXPORT_TO_GCS = "true"
TARGET_METRIC = REBUFFERING.request_key

# What `compare_with=1WEEK` adds to the answer: the seven days before the requested window.
COMPARISON_DAYS = 7
MINUTES_PER_DAY = 1440

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0

# Paths Django redirects an unauthenticated request to.
LOGIN_MARKERS = ("/login", "/accounts/login", "/sso", "/oauth2", "/saml")


class CascadaError(Exception):
    """CASCADA could not answer this call. The message is what the operator is shown."""


def window_for(thresholds: Thresholds, now: dt.datetime | None = None) -> Window:
    """The current window: midnight UTC `cascada_window_days` ago, to this minute.

    The start is pinned to midnight so two calls made an hour apart cover the same days and
    their averages are comparable; the end is the moment of the call, because the newest
    minute is the one an operator is asking about.
    """
    moment = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    end = moment.replace(second=0, microsecond=0)
    midnight = end.replace(hour=0, minute=0)
    start = midnight - dt.timedelta(days=max(1, thresholds.cascada_window_days))
    return Window(start=start, end=end)


def row_limit(window: Window, margin: int) -> int:
    """How many rows the answer needs room for.

    One row per minute of the requested window, plus one per minute of the comparison week
    `compare_with` adds on top, plus headroom for the window growing between the request and
    the answer.
    """
    return window.minutes + COMPARISON_DAYS * MINUTES_PER_DAY + max(0, margin)


def build_url(
    *,
    channel_name: str,
    channel_id: str,
    window: Window,
    limit: int,
    base_url: str | None = None,
    metric: Metric = REBUFFERING,
) -> str:
    """The realtime URL for one channel over one window, for one metric.

    `urlencode` produces `+` for the spaces in the channel group name and the platform, and
    `target_metrics%5B%5D` for the bracketed parameter, which is what the dashboard sends.
    The metric is the only parameter that differs between the rebuffering and error calls.
    """
    settings = get_settings()
    base = (base_url or settings.cascada_base_url).rstrip("/")
    query = urlencode(
        [
            ("channel_group_name", CHANNEL_GROUP_NAME),
            ("channel_name", channel_name),
            ("channel_id", channel_id),
            ("channel_country", CHANNEL_COUNTRY),
            ("platform", PLATFORM),
            ("model", MODEL),
            ("from", int(window.start.timestamp())),
            ("to", int(window.end.timestamp())),
            ("limit", int(limit)),
            ("compare_with", COMPARE_WITH),
            ("target_metrics[]", metric.request_key),
            ("export_to_gcs", EXPORT_TO_GCS),
        ]
    )
    return f"{base}{API_PATH}?{query}"


def _looks_like_login(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return any(marker in path for marker in LOGIN_MARKERS)


def _auth_failure(status: int, final_url: str) -> str | None:
    """The sentence to raise when a response means the session is no longer good."""
    if status in (401, 403):
        return f"CASCADA answered HTTP {status}, which means the session is not accepted."
    if 300 <= status < 400 and _looks_like_login(final_url):
        return "CASCADA redirected the call to its login page, which means the session expired."
    if 200 <= status < 300 and _looks_like_login(final_url):
        return "CASCADA served its login page instead of data, which means the session expired."
    return None


async def fetch_series(
    *,
    channel_name: str,
    channel_id: str,
    auth: CascadaAuth,
    thresholds: Thresholds,
    window: Window | None = None,
    fetcher: Fetcher | None = None,
    metric: Metric = REBUFFERING,
) -> ChannelSeries:
    """Fetch and parse one channel's series for one metric, rebuffering unless told otherwise.

    Raises `CascadaAuthError` when the session is the problem and `CascadaError` for anything
    else, both carrying a sentence the operator can act on.
    """
    settings = get_settings()
    span = window or window_for(thresholds)
    url = build_url(
        channel_name=channel_name,
        channel_id=channel_id,
        window=span,
        limit=row_limit(span, settings.cascada_row_limit_margin),
        metric=metric,
    )
    headers = auth.headers()

    own = fetcher is None
    client = fetcher or Fetcher(
        timeout_s=settings.cascada_timeout_s,
        per_host_connections=settings.rba_per_host_connections,
    )
    try:
        last_error = ""
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            result = await client.fetch(url, headers=headers)

            reason = _auth_failure(result.status, result.final_url)
            if reason is not None:
                # A rejected session is not retried: every attempt would be rejected the same
                # way, and the operator has to paste a new one.
                raise CascadaAuthError(
                    f"{reason} Paste a fresh session in the Settings tab, then run this again."
                )

            if result.ok:
                return _parse_body(
                    result.text, channel_name=channel_name, window=span, metric=metric
                )

            last_error = (
                f"the call failed on the wire ({result.error})"
                if result.error is not None
                else f"CASCADA answered HTTP {result.status}"
            )
            # A 4xx is the request being wrong, and repeating it cannot make it right.
            if result.error is None and 400 <= result.status < 500:
                break
            if attempt < RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_BACKOFF_S * attempt)

        raise CascadaError(
            f"CASCADA did not serve the {metric.label} data for {channel_name}: {last_error}."
        )
    finally:
        if own:
            await client.aclose()


def _parse_body(
    body: str, *, channel_name: str, window: Window, metric: Metric = REBUFFERING
) -> ChannelSeries:
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise CascadaError(
            f"CASCADA returned {len(body)} byte(s) for {channel_name} that do not parse as "
            f"JSON: {exc}."
        ) from exc

    try:
        series = parse(payload, requested=window, metric=metric)
    except CascadaParseError as exc:
        raise CascadaError(str(exc)) from exc

    if series.unexpected_categories:
        # A category nobody has seen before is reported rather than dropped: it may be data
        # that belongs in one of the two series.
        logger.warning(
            "CASCADA returned unknown date_category value(s) %s for %s",
            series.unexpected_categories,
            channel_name,
        )
    if not series.origin and series.comparison:
        raise CascadaError(
            f"CASCADA returned {len(series.comparison)} {COMPARISON} row(s) for "
            f"{channel_name} and no row for the requested window, so there is no current "
            "measurement to average."
        )
    return series


async def validate(auth: CascadaAuth, thresholds: Thresholds) -> str:
    """Prove a session works, with one call for one channel over one minute.

    The shortest window the API will accept keeps validation cheap, and the call reaching
    CASCADA at all is what is being tested — an empty series still proves the session.
    """
    now = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)
    span = Window(start=now - dt.timedelta(minutes=1), end=now)
    settings = get_settings()
    url = build_url(
        channel_name="",
        channel_id="",
        window=span,
        limit=row_limit(span, settings.cascada_row_limit_margin),
    )
    fetcher = Fetcher(
        timeout_s=settings.cascada_timeout_s,
        per_host_connections=settings.rba_per_host_connections,
    )
    try:
        result = await fetcher.fetch(url, headers=auth.headers())
    finally:
        await fetcher.aclose()

    reason = _auth_failure(result.status, result.final_url)
    if reason is not None:
        raise CascadaAuthError(reason)
    if result.error is not None:
        raise CascadaError(f"The call to CASCADA failed on the wire: {result.error}.")
    if not result.ok:
        raise CascadaError(f"CASCADA answered HTTP {result.status} to the validation call.")
    return f"Validated against {API_PATH} at {dt.datetime.now(dt.UTC).isoformat()}."


def describe_window(window: Window) -> str:
    """One line naming the window, for a report header."""
    return (
        f"{window.start.isoformat()} to {window.end.isoformat()} UTC "
        f"({window.days:.2f} days, {window.minutes} minutes)"
    )


def channel_payload(
    *, series: ChannelSeries, stats: Any, channel: dict[str, str]
) -> dict[str, Any]:
    """The per-channel response body, shared by the live path and the cached one."""
    return {
        "channel": channel,
        "series": series.as_dict(),
        "stats": stats.as_dict(),
        "origin": [p.as_dict() for p in series.origin],
        "comparison": [p.as_dict() for p in series.comparison],
    }
