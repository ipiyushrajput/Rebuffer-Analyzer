"""The work the CASCADA Data tab asks for.

Two things happen here: one channel's window is produced — from the store when it is fresh
enough, from CASCADA when it is not — and a whole country is walked channel by channel.

The country's channel list comes from `app.tvplus.catalogue`, the same module the All
channels tab reads, called rather than copied: the `dbconnect` mapping, the URL builder, the
row parser and the playback-URL cleanup all stay in one place.

A scan is hundreds of CASCADA calls, so it runs as a background task behind a semaphore, with
progress, cancellation and a list of the channels that failed. A channel that fails is named,
never dropped: an operator has to be able to see that a country report is missing six
channels and which six.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.cascada import client, store
from app.cascada.auth import CascadaAuth, CascadaAuthError, mark_stored_session, resolve_auth
from app.cascada.series import Window, summarise
from app.cascada.store import ChannelWindow
from app.config import Thresholds, get_settings, get_thresholds
from app.net.fetcher import Fetcher
from app.tvplus import catalogue as cat

logger = logging.getLogger(__name__)

# The environment the tab reads. CASCADA reports on what viewers watch, which is production.
ENVIRONMENT = "PRD"

# A country with more pages than this is not walked further; no TV Plus country is close.
MAX_PAGES = 100


class CascadaScanError(Exception):
    """The scan could not be set up. The message is what the operator is shown."""


async def channel_window(
    *,
    service_id: str,
    channel_name: str,
    country: str,
    thresholds: Thresholds | None = None,
    window: Window | None = None,
    auth: CascadaAuth | None = None,
    fetcher: Fetcher | None = None,
    refresh: bool = False,
) -> ChannelWindow:
    """One channel's rebuffering window, from the store when it is fresh, else from CASCADA."""
    limits = thresholds or get_thresholds()
    span = window or client.window_for(limits)

    if not refresh:
        cached = await store.read(service_id, span, limits.cascada_cache_ttl_minutes)
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
    )
    entry = store.to_window(
        service_id=service_id,
        channel_name=channel_name,
        country=country,
        series=series,
        stats=summarise(series, limits),
        window=span,
    )
    await store.write(entry)
    return entry


async def country_channels(country: str) -> list[cat.Channel]:
    """Every channel the catalogue lists for one country, across every page.

    The catalogue is read through its own module, so the environment mapping, the paging and
    the playback-URL cleanup are the ones the All channels tab already uses.
    """
    settings = get_settings()
    stamp = cat.today_stamp()
    channels: list[cat.Channel] = []

    fetcher = Fetcher(timeout_s=20.0, per_host_connections=settings.rba_per_host_connections)
    try:
        for page in range(1, MAX_PAGES + 1):
            url = cat.build_url(code=country, environment=ENVIRONMENT, page=page, today=stamp)
            result = await fetcher.fetch(url)
            if result.error is not None:
                raise CascadaScanError(f"The channel catalogue did not answer: {result.error}.")
            if not result.ok:
                raise CascadaScanError(
                    f"The channel catalogue answered HTTP {result.status} for "
                    f"{country.upper()} page {page}."
                )
            parsed = cat.parse_page(
                result.text, page=page, page_size=cat.PAGE_SIZE, url=url, today=stamp
            )
            channels.extend(parsed.channels)
            if not parsed.has_next:
                break
    except cat.CatalogueError as exc:
        raise CascadaScanError(str(exc)) from exc
    finally:
        await fetcher.aclose()

    return channels


@dataclass(slots=True)
class Failure:
    """A channel the scan could not measure, and the reason, so nothing is silently missing."""

    service_id: str
    channel_name: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "service_id": self.service_id,
            "channel_name": self.channel_name,
            "reason": self.reason,
        }


@dataclass(slots=True)
class Scan:
    """One country-wide scan, its progress and what it has found so far."""

    id: str
    country: str
    window: Window
    total: int
    threshold_pct: float
    status: str = "RUNNING"
    done: int = 0
    results: dict[str, ChannelWindow] = field(default_factory=dict)
    failures: list[Failure] = field(default_factory=list)
    # The playback URL the catalogue lists for each channel, carried so the rebuffering
    # channels can be handed to Bulk analysis without fetching the catalogue a second time.
    # A channel the catalogue lists with no CNTN_URI has an empty string here.
    urls: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    started_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    finished_at: dt.datetime | None = None

    @property
    def above(self) -> list[ChannelWindow]:
        """The rebuffering channels: those whose average is above the threshold.

        The average alone decides. A channel with one bad minute and a clean week is not a
        rebuffering channel, which is the distinction the whole scan exists to make.
        """
        found = [w for w in self.results.values() if w.stats.above_threshold]
        found.sort(key=lambda w: w.stats.average_pct or 0.0, reverse=True)
        return found

    @property
    def complete(self) -> bool:
        return self.status == "COMPLETED" and not self.failures

    def as_dict(self) -> dict[str, Any]:
        above = self.above
        return {
            "scan_id": self.id,
            "country": self.country,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "progress": self.done / self.total if self.total else 0.0,
            "window": self.window.as_dict(),
            "threshold_pct": self.threshold_pct,
            "above": [{**w.row(), "playback_url": self.urls.get(w.service_id, "")} for w in above],
            "above_count": len(above),
            # A rebuffering channel with no playback URL cannot be analysed, and saying how
            # many is what keeps the Bulk hand-off honest about what it left behind.
            "above_analysable_count": sum(1 for w in above if self.urls.get(w.service_id)),
            "measured_count": len(self.results),
            "below_count": len(self.results) - len(above),
            "failures": [f.as_dict() for f in self.failures],
            "error": self.error,
            "complete": self.complete,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


# Scans in flight. A scan is a read: losing the registry on a restart costs a re-run, and
# every measurement it produced is already in the store.
_scans: dict[str, Scan] = {}
_tasks: dict[str, asyncio.Task[None]] = {}


def get_scan(scan_id: str) -> Scan | None:
    return _scans.get(scan_id)


def latest_scan(country: str) -> Scan | None:
    """The most recent scan for a country, so a reopened tab finds the one already running."""
    for scan in sorted(_scans.values(), key=lambda s: s.started_at, reverse=True):
        if scan.country == country:
            return scan
    return None


async def start_scan(country: str, concurrency: int | None = None) -> Scan:
    """Walk one country, measuring every channel it lists."""
    code = cat.country(country).code  # Raises CatalogueError on a country that is not served.
    thresholds = get_thresholds()
    auth = await resolve_auth()
    if not auth.describe().configured:
        raise CascadaAuthError(
            "No CASCADA session is configured, so a scan would fail on its first channel. "
            "Paste a session in the Settings tab first."
        )

    channels = await country_channels(code)
    if not channels:
        raise CascadaScanError(f"The catalogue lists no channel for {code}.")

    scan = Scan(
        id=uuid.uuid4().hex,
        country=code,
        window=client.window_for(thresholds),
        total=len(channels),
        threshold_pct=thresholds.cascada_rebuffering_threshold_pct,
        urls={channel.service_id: channel.playback_url for channel in channels},
    )
    _scans[scan.id] = scan
    _tasks[scan.id] = asyncio.create_task(
        _run_scan(scan, channels, thresholds, auth, concurrency),
        name=f"rba:cascada-scan:{scan.id}",
    )
    return scan


async def _run_scan(
    scan: Scan,
    channels: list[cat.Channel],
    thresholds: Thresholds,
    auth: CascadaAuth,
    concurrency: int | None,
) -> None:
    settings = get_settings()
    limit = max(1, concurrency or thresholds.cascada_scan_concurrency)
    semaphore = asyncio.Semaphore(limit)

    # Whatever this country already holds inside its TTL is not fetched again.
    cached = await store.read_country(
        scan.country, scan.window, thresholds.cascada_cache_ttl_minutes
    )

    fetcher = Fetcher(
        timeout_s=settings.cascada_timeout_s,
        per_host_connections=settings.rba_per_host_connections,
    )

    async def measure(channel: cat.Channel) -> None:
        async with semaphore:
            try:
                held = cached.get(channel.service_id)
                scan.results[channel.service_id] = held or await channel_window(
                    service_id=channel.service_id,
                    channel_name=channel.name,
                    country=channel.country or scan.country,
                    thresholds=thresholds,
                    window=scan.window,
                    auth=auth,
                    fetcher=fetcher,
                )
            except asyncio.CancelledError:
                raise
            except CascadaAuthError as exc:
                # The session died mid-scan. Every remaining channel would fail the same way,
                # so the scan stops and says so rather than logging hundreds of failures.
                scan.error = str(exc)
                scan.status = "AUTH_FAILED"
                raise
            except Exception as exc:
                scan.failures.append(
                    Failure(
                        service_id=channel.service_id,
                        channel_name=channel.name,
                        reason=str(exc) or type(exc).__name__,
                    )
                )
            finally:
                scan.done += 1

    try:
        await asyncio.gather(*(measure(channel) for channel in channels))
        scan.status = "COMPLETED"
    except asyncio.CancelledError:
        scan.status = "CANCELLED"
        raise
    except CascadaAuthError:
        await mark_stored_session(valid=False, detail=scan.error or "The session was rejected.")
    except Exception as exc:
        scan.status = "FAILED"
        scan.error = f"{type(exc).__name__}: {exc}"
        logger.exception("the CASCADA scan of %s failed", scan.country)
    finally:
        scan.finished_at = dt.datetime.now(dt.UTC)
        await fetcher.aclose()


async def cancel_scan(scan_id: str) -> Scan | None:
    """Stop a scan. What it has already measured stays, in the scan and in the store."""
    scan = _scans.get(scan_id)
    if scan is None:
        return None
    task = _tasks.get(scan_id)
    if task is not None and not task.done():
        task.cancel()
    if scan.status == "RUNNING":
        scan.status = "CANCELLED"
        scan.finished_at = dt.datetime.now(dt.UTC)
    return scan
