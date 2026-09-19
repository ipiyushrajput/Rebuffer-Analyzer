"""The batch pipeline: list, scan, select, analyse, report.

One `asyncio.Task` per batch, the same shape the bulk router already uses. What makes this
different from bulk is that nothing about the run lives in memory: every phase writes its
progress to `batches` and every channel's state to `batch_items` before moving on, so a
reload shows live progress, a restart resumes from the first incomplete phase, and the report
can be rebuilt afterwards without re-running anything.

A channel that fails is recorded with its error and the batch carries on. A batch that
reaches its runtime cap stops selecting new channels and says how many it did not reach —
never silently truncated.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from typing import Any

from app.batch import store
from app.batch.settings import BatchSettings
from app.cascada import client as cascada_client
from app.cascada import service as cascada
from app.cascada.auth import CascadaAuthError
from app.cascada.series import Window
from app.cascada.service import CascadaScanError
from app.config import get_thresholds
from app.jobs.manager import job_manager
from app.tvplus import catalogue as cat

logger = logging.getLogger(__name__)

# Batches running in this process. A batch not in here after a restart is what the startup
# sweep picks up; the row, not this dict, is the truth about whether it is running.
_tasks: dict[str, asyncio.Task[None]] = {}
_cancelled: set[str] = set()


class BatchError(Exception):
    """The batch could not be set up. The message is what the operator is shown."""


async def estimate(country: str, settings: BatchSettings) -> dict[str, Any]:
    """What starting a batch for this country would involve, before committing to it."""
    code = cat.country(country).code
    channels = await cascada.country_channels(code)
    listed = len(channels)
    # Every listed channel is scanned; only the above-threshold ones are analysed, and how
    # many that is cannot be known until the scan has run.
    fits = settings.channels_within_runtime()
    return {
        "country": code,
        "channels_listed": listed,
        "analysis_duration_minutes": settings.analysis_duration_minutes,
        "analysis_concurrency": settings.analysis_concurrency,
        "max_runtime_minutes": settings.max_runtime_minutes,
        "channels_within_runtime": fits,
        # The worst case: every listed channel turns out to be above threshold.
        "worst_case_runtime_minutes": settings.estimated_runtime_minutes(listed),
        "window_days": get_thresholds().cascada_window_days,
    }


async def start(country: str, kind: str = store.MANUAL) -> dict[str, Any]:
    """Create a batch and set it running. Refuses a second batch for the same country."""
    code = cat.country(country).code
    existing = await store.running_for(code)
    if existing is not None:
        raise BatchError(
            f"A batch for {code} is already {existing['status'].lower()} "
            f"(started {existing['created_at']}). Open that one instead."
        )

    from app.batch import settings as batch_settings

    configured = await batch_settings.load()
    batch_id = uuid.uuid4().hex
    await store.create(batch_id=batch_id, country=code, kind=kind, snapshot=configured.snapshot())
    _tasks[batch_id] = asyncio.create_task(_run(batch_id), name=f"rba:batch:{batch_id}")
    return {"id": batch_id, "country": code, "kind": kind, "status": store.QUEUED}


async def cancel(batch_id: str) -> dict[str, Any] | None:
    """Stop after the channel in flight, keeping everything measured so far."""
    current = await store.read(batch_id)
    if current is None:
        return None
    if current["status"] in store.FINISHED_STATES:
        return current

    _cancelled.add(batch_id)
    await store.log(batch_id, "Cancellation requested; stopping after the current channel.")
    task = _tasks.get(batch_id)
    if task is not None and not task.done():
        # The task checks the flag between channels, so the run stops cleanly rather than
        # abandoning a channel mid-analysis.
        await asyncio.sleep(0)
    return await store.read(batch_id)


def is_cancelled(batch_id: str) -> bool:
    return batch_id in _cancelled


# -- the pipeline ------------------------------------------------------------


async def _run(batch_id: str, resume: bool = False) -> None:
    """One batch, end to end. Every failure it can survive is recorded and survived."""
    current = await store.read(batch_id)
    if current is None:
        return
    settings = BatchSettings.from_snapshot(current["settings"])
    country = current["country"]
    deadline = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=settings.max_runtime_minutes)

    try:
        await store.update(
            batch_id,
            status=store.SCANNING,
            phase="SCANNING",
            started_at=dt.datetime.now(dt.UTC),
        )
        if not resume or not await store.items_for(batch_id):
            await _scan(batch_id, country, settings)
        else:
            await store.log(batch_id, "Resuming after a restart; the scan already completed.")

        if is_cancelled(batch_id):
            return await _finish(batch_id, store.CANCELLED, "Cancelled before analysis began.")

        await _analyse(batch_id, settings, deadline)

        if is_cancelled(batch_id):
            return await _finish(batch_id, store.CANCELLED, "Cancelled during analysis.")

        await _report(batch_id)
        await _follow_up(batch_id, current["kind"], settings)

        failed = (await store.read(batch_id) or {}).get("channels_failed", 0)
        await _finish(
            batch_id,
            store.COMPLETED_WITH_ERRORS if failed else store.COMPLETED,
            f"{failed} channel(s) failed." if failed else "Every selected channel was analysed.",
        )
    except asyncio.CancelledError:
        await _finish(batch_id, store.CANCELLED, "The batch task was cancelled.")
        raise
    except (CascadaAuthError, CascadaScanError) as exc:
        # Both carry a sentence the operator can act on, so the class name would be noise.
        await _finish(batch_id, store.FAILED, str(exc))
    except Exception as exc:
        logger.exception("batch %s failed", batch_id)
        await _finish(batch_id, store.FAILED, f"{type(exc).__name__}: {exc}")
    finally:
        _cancelled.discard(batch_id)
        _tasks.pop(batch_id, None)


async def _finish(batch_id: str, status: str, reason: str) -> None:
    await store.update(
        batch_id,
        status=status,
        phase=status,
        finished_at=dt.datetime.now(dt.UTC),
        error=reason if status == store.FAILED else None,
    )
    await store.log(
        batch_id, f"Batch {status}: {reason}", "ERROR" if status == store.FAILED else "INFO"
    )


# How often the batch copies the running scan's progress onto its own row. A scan of a large
# country is hundreds of calls; a counter that only moves when the whole scan finishes is
# indistinguishable from a scan that has hung.
SCAN_POLL_S = 2.0


async def _scan(batch_id: str, country: str, settings: BatchSettings) -> None:
    """Measure every channel in the country and keep the ones above the threshold.

    This drives `cascada.start_scan` — the same scan the CASCADA Data tab runs — rather than
    walking the channels itself. That is not only the convention: the module's scan reads the
    country's stored windows in one query, shares one HTTP client across every channel, and
    counts each channel as it lands. A second loop here had none of those, so a large country
    opened hundreds of clients, re-read the cache a row at a time, and reported nothing at all
    until the last channel returned.
    """
    thresholds = get_thresholds()
    scan = await cascada.start_scan(country)

    await store.update(
        batch_id,
        channels_listed=scan.total,
        window_from=int(scan.window.start.timestamp()),
        window_to=int(scan.window.end.timestamp()),
    )
    await store.log(
        batch_id,
        f"The catalogue lists {scan.total} channel(s) for {country}. Scanning them against "
        f"CASCADA over {cascada_client.describe_window(scan.window)}, "
        f"{thresholds.cascada_scan_concurrency} at a time.",
    )

    # The scan runs as its own task. This loop is what makes its progress the batch's, so the
    # row a reloaded tab reads moves while the scan is still working.
    reported = -1
    try:
        while scan.finished_at is None:
            if is_cancelled(batch_id):
                await cascada.cancel_scan(scan.id)
                break
            if scan.done != reported:
                reported = scan.done
                await store.update(
                    batch_id, channels_scanned=scan.done, channels_failed=len(scan.failures)
                )
            await asyncio.sleep(SCAN_POLL_S)
    except asyncio.CancelledError:
        await cascada.cancel_scan(scan.id)
        raise

    await store.update(batch_id, channels_scanned=scan.done, channels_failed=len(scan.failures))

    if scan.status == "AUTH_FAILED":
        raise CascadaAuthError(
            scan.error
            or "CASCADA rejected the session during the scan. Paste a fresh one in Settings."
        )
    if scan.status == "FAILED":
        raise CascadaScanError(scan.error or "The CASCADA scan failed.")
    if scan.status == "CANCELLED":
        # What it measured before it stopped is real, and the batch keeps it. Saying so is
        # what stops a partial selection from reading as the whole country.
        await store.log(
            batch_id,
            f"The scan was cancelled after {scan.done} of {scan.total} channel(s). The "
            "selection below covers only what it measured.",
            "WARN",
        )

    for failure in scan.failures:
        await store.log(
            batch_id,
            f"{failure.service_id} {failure.channel_name}: CASCADA did not answer "
            f"({failure.reason}).",
            "WARN",
        )

    # `scan.above` selects on the average and is already sorted worst first — the same rule
    # and the same order the CASCADA Data tab and its country report use.
    measured: list[dict[str, Any]] = [
        {
            "service_id": entry.service_id,
            "channel_name": entry.channel_name,
            "country": entry.country or country,
            "playback_url": scan.urls.get(entry.service_id, ""),
            "average_pct": entry.stats.average_pct,
            "max_pct": entry.stats.max_pct,
            "minutes_above": entry.stats.minutes_above,
            "status": "PENDING" if scan.urls.get(entry.service_id) else "NO_URL",
        }
        for entry in scan.above
    ]

    await store.add_items(batch_id, measured)
    await store.update(batch_id, channels_above=len(measured))
    await store.log(
        batch_id,
        f"{scan.done} channel(s) scanned, {len(measured)} above "
        f"{thresholds.cascada_rebuffering_threshold_pct} %"
        + (f", {len(scan.failures)} could not be measured." if scan.failures else "."),
    )

    without_url = [item for item in measured if not item["playback_url"]]
    if without_url:
        await store.log(
            batch_id,
            f"{len(without_url)} above-threshold channel(s) carry no playback URL and cannot "
            "be analysed: " + ", ".join(item["service_id"] for item in without_url),
            "WARN",
        )


async def _analyse(batch_id: str, settings: BatchSettings, deadline: dt.datetime) -> None:
    """Run the analysis engine over each selected channel, a few at a time."""
    pending = await store.items_for(batch_id, statuses=("PENDING",))
    if not pending:
        await store.log(batch_id, "No channel was above the threshold; nothing to analyse.")
        return

    await store.update(batch_id, status=store.ANALYSING, phase="ANALYSING")
    await store.log(
        batch_id,
        f"Analysing {len(pending)} channel(s), {settings.analysis_concurrency} at a time, "
        f"{settings.analysis_duration_minutes} minute(s) each.",
    )

    semaphore = asyncio.Semaphore(max(1, settings.analysis_concurrency))
    done = 0
    failed = (await store.read(batch_id) or {}).get("channels_failed", 0)
    skipped: list[str] = []

    async def analyse(item: dict[str, Any]) -> None:
        nonlocal done, failed
        async with semaphore:
            if is_cancelled(batch_id):
                return
            if dt.datetime.now(dt.UTC) >= deadline:
                skipped.append(item["service_id"])
                await store.update_item(
                    batch_id,
                    item["service_id"],
                    status="SKIPPED",
                    error="The batch reached its maximum runtime before this channel was reached.",
                )
                return

            await store.update_item(batch_id, item["service_id"], status="ANALYSING")
            try:
                handle = await job_manager.submit(
                    job_type="aging",
                    playback_url=item["playback_url"],
                    channel_name=f"{item['channel_name']} · {item['service_id']}",
                    options=_options(settings),
                )
                await store.update_item(batch_id, item["service_id"], job_id=handle.id)
                if handle.task is not None:
                    await handle.task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed += 1
                await store.update_item(
                    batch_id,
                    item["service_id"],
                    status="FAILED",
                    error=f"{type(exc).__name__}: {exc}",
                )
                await store.log(
                    batch_id, f"{item['service_id']} {item['channel_name']} failed: {exc}", "WARN"
                )
                return
            else:
                await store.update_item(batch_id, item["service_id"], status="ANALYSED")
            finally:
                done += 1
                await store.update(
                    batch_id, channels_analysed=done, channels_failed=failed, phase="ANALYSING"
                )

    await asyncio.gather(*(analyse(item) for item in pending))

    if skipped:
        await store.log(
            batch_id,
            f"{len(skipped)} channel(s) were not reached inside the "
            f"{settings.max_runtime_minutes}-minute cap: " + ", ".join(skipped),
            "WARN",
        )


def _options(settings: BatchSettings) -> Any:
    """The analysis options a batch channel runs with."""
    from app.analysis.engine import SessionOptions

    return SessionOptions(
        duration_s=settings.analysis_duration_s(),
        record_evidence=False,
    )


async def _report(batch_id: str) -> None:
    """Build and store the batch's report, so it can be downloaded long after the run."""
    await store.update(batch_id, status=store.REPORTING, phase="REPORTING")
    from app.batch import reporting

    try:
        await reporting.build(batch_id)
        await store.log(batch_id, "Report generated and stored.")
    except Exception as exc:
        # A report that cannot be built must not lose the analysis that produced it.
        logger.warning("batch %s report failed: %s", batch_id, type(exc).__name__)
        await store.log(batch_id, f"The report could not be generated: {exc}", "ERROR")


async def _follow_up(batch_id: str, kind: str, settings: BatchSettings) -> None:
    """Start continuous aging, which only a scheduled batch does."""
    if kind != store.SCHEDULED or not settings.aging_enabled:
        return
    from app.batch import aging as batch_aging

    await batch_aging.start_for(batch_id, settings)


# -- restart -----------------------------------------------------------------


async def sweep_unfinished() -> int:
    """Pick up batches the process was running when it stopped.

    A batch is never left in a running state with nothing running it. One whose settings can
    be read is resumed from the first incomplete phase — the items are rows, so resuming means
    running the ones still pending. One that cannot be resumed is failed with the reason.
    """
    resumed = 0
    for current in await store.unfinished():
        batch_id = current["id"]
        try:
            BatchSettings.from_snapshot(current["settings"])
        except Exception as exc:
            await _finish(
                batch_id,
                store.FAILED,
                f"The analyzer restarted during {current['phase']} and this batch's settings "
                f"could not be read back ({type(exc).__name__}), so it cannot be resumed.",
            )
            continue

        await store.log(
            batch_id,
            f"The analyzer restarted during {current['phase']}; resuming this batch.",
            "WARN",
        )
        _tasks[batch_id] = asyncio.create_task(
            _run(batch_id, resume=True), name=f"rba:batch:{batch_id}"
        )
        resumed += 1

    if resumed:
        logger.info("resumed %d automated batch(es) after restart", resumed)
    return resumed


def window_of(current: dict[str, Any]) -> Window | None:
    """The measurement window a batch covered, read back from its row."""
    start, end = current.get("window_from"), current.get("window_to")
    if not start or not end:
        return None
    return Window(
        start=dt.datetime.fromisoformat(str(start)), end=dt.datetime.fromisoformat(str(end))
    )
