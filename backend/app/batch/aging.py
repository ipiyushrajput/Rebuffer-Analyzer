"""Continuous aging: what happens to a country's worst channels after a scheduled batch.

A batch measures a week that has already happened. Aging watches the week that is about to,
so the next report can say what the analyzer saw at the moments viewers were rebuffering —
which is the only way the two halves of §9 ever meet.

Three rules shape it. Only a **scheduled** batch starts aging, because a manual batch is
someone looking at a country, not a commitment to watch it for a week. Only the worst few
channels age at once, because each run polls a ladder continuously for days and the host has
a limit; the rest are named rather than queued behind a run that lasts a week. And the
previous week's runs are stopped before this week's start, so they cannot pile up.
"""

from __future__ import annotations

import logging

from app.batch import store
from app.batch.settings import BatchSettings
from app.jobs.manager import job_manager

logger = logging.getLogger(__name__)

# The statuses whose channels are worth watching: the ones that actually analysed.
ANALYSED = ("ANALYSED", "COMPLETED")


async def start_for(batch_id: str, settings: BatchSettings) -> int:
    """Put this batch's worst channels into aging for the configured span."""
    current = await store.read(batch_id)
    if current is None:
        return 0
    country = current["country"]

    stopped = await stop_previous(country, batch_id)
    if stopped:
        await store.log(
            batch_id,
            f"Stopped {stopped} aging run(s) from the previous batch for {country} before "
            "starting this week's.",
        )

    items = [
        item
        for item in await store.items_for(batch_id)
        if item["status"] in ANALYSED and item["playback_url"]
    ]
    if not items:
        await store.log(batch_id, "No analysed channel carries a playback URL, so none is aging.")
        return 0

    # `items_for` returns worst first, so the cap keeps the channels that matter most.
    limit = max(1, settings.aging_max_concurrent)
    chosen, skipped = items[:limit], items[limit:]

    from app.analysis.engine import SessionOptions

    duration_s = settings.aging_duration_days * 24 * 60 * 60.0
    started = 0
    for item in chosen:
        try:
            handle = await job_manager.submit(
                job_type="aging",
                playback_url=item["playback_url"],
                channel_name=f"{item['channel_name']} · {item['service_id']} · aging",
                options=SessionOptions(duration_s=duration_s, record_evidence=True),
            )
        except Exception as exc:
            await store.log(
                batch_id,
                f"{item['service_id']} {item['channel_name']} could not be put into aging: {exc}",
                "WARN",
            )
            continue
        await store.update_item(batch_id, item["service_id"], aging_job_id=handle.id)
        started += 1

    await store.log(
        batch_id,
        f"{started} channel(s) are aging for {settings.aging_duration_days} day(s), "
        f"the worst first, up to the limit of {limit}.",
    )
    if skipped:
        # Named, not dropped: a reader has to be able to see which channels nobody watched.
        await store.log(
            batch_id,
            f"{len(skipped)} above-threshold channel(s) were not aged because the limit of "
            f"{limit} concurrent run(s) was reached: "
            + ", ".join(f"{item['service_id']} ({item['average_pct']:.3f} %)" for item in skipped),
            "WARN",
        )
    return started


async def stop_previous(country: str, before: str) -> int:
    """Cancel the aging runs the previous batch for this country started.

    Without this a weekly schedule would leave a new set of week-long runs behind every
    Monday and the host would carry all of them at once.
    """
    previous = await store.previous_batch(country, before)
    if previous is None:
        return 0

    stopped = 0
    for item in await store.items_for(previous["id"]):
        job_id = item.get("aging_job_id")
        if not job_id:
            continue
        handle = job_manager.get(job_id)
        if handle is None or handle.status in ("COMPLETED", "FAILED", "CANCELLED"):
            continue
        try:
            await job_manager.cancel(job_id)
            stopped += 1
        except Exception as exc:
            logger.warning(
                "aging run %s from batch %s was not cancelled: %s",
                job_id,
                previous["id"],
                type(exc).__name__,
            )
    return stopped
