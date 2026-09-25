"""Turning a finished batch into the report, and the summaries that fill it.

This runs after the analyses, and everything it needs is already in the database — the batch,
its channels, the findings each analysis produced, and the CASCADA series each channel was
selected on. That is deliberate: a report can be regenerated at any time without re-running a
single channel, which is what makes a download still work weeks later.

Where a channel was aging through the measured week, the aging run's findings are correlated
against the minutes the rebuffering ratio was high, and the result goes into the summary and
onto the correlation sheet.

**Which aging run.** A scheduled batch starts its aging runs *after* its report, and they watch
the week that follows. So the aging run that watched the week this batch measured is the
*previous* batch's, for the same country and channel — that is the one correlated here. A
channel's own `aging_job_id` is used when it has one (a report rebuilt later), else the
previous batch's.

**Which minutes.** Spike windows need per-minute data. A realtime batch without an aging run
keeps what it always did: the spikes in the window it was selected on. Otherwise — a channel
with an aging run to compare, or any channel of a historical batch, whose selection data is
daily — the realtime per-minute window is read at report time for that channel alone, which
ends now and so covers the aging run up to this moment. When the aging run started before that
window, the report says so and does not correlate: part of the run would have nothing to be
compared against.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import select

from app.api.job_views import finding_payload, incident_payload
from app.batch import correlate, exports, store, summary
from app.batch.settings import BatchSettings
from app.cascada import service as cascada_service
from app.cascada import store as cascada_store
from app.cascada.series import Window
from app.db import session as db_session
from app.db.models import Finding as FindingRow
from app.db.models import Incident as IncidentRow
from app.db.models import Job as JobRow
from app.reports import service

logger = logging.getLogger(__name__)


async def _job_evidence(job_id: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The findings and incidents one analysis recorded."""
    if not job_id:
        return [], []
    async with db_session.session_scope() as session:
        findings = (
            (await session.execute(select(FindingRow).where(FindingRow.job_id == job_id)))
            .scalars()
            .all()
        )
        incidents = (
            (await session.execute(select(IncidentRow).where(IncidentRow.job_id == job_id)))
            .scalars()
            .all()
        )
        return (
            [finding_payload(row) for row in findings],
            [incident_payload(row) for row in incidents],
        )


async def _aging_period(job_id: str) -> tuple[dt.datetime, dt.datetime] | None:
    """When an aging run watched: from its start to its end, or to now while it runs."""
    async with db_session.session_scope() as session:
        row = await session.get(JobRow, job_id)
        if row is None or row.started_at is None:
            return None
        start = correlate.as_utc(row.started_at)
        end = correlate.as_utc(row.finished_at) if row.finished_at else dt.datetime.now(dt.UTC)
        return start, end


async def _previous_aging(batch: dict[str, Any]) -> tuple[str | None, dict[str, str]]:
    """The previous batch for this country, and each channel's aging run in it."""
    previous = await store.previous_batch(batch["country"], batch["id"])
    if previous is None:
        return None, {}
    return previous["id"], {
        item["service_id"]: item["aging_job_id"]
        for item in await store.items_for(previous["id"])
        if item.get("aging_job_id")
    }


async def _correlation(
    item: dict[str, Any],
    window: Window,
    settings: BatchSettings,
    threshold_pct: float,
    *,
    source: str = "realtime",
    aging_job_id: str | None = None,
    aging_batch_id: str | None = None,
) -> dict[str, Any]:
    """The spike windows for this channel, and what the aging run that watched them captured.

    A channel without an aging run still has its spike windows, and saying so — rather than
    omitting them — is what tells a reader the week had spikes nobody was watching.
    """
    live = source != "realtime" or aging_job_id is not None
    if not live:
        stored = await cascada_store.read(item["service_id"], window, ttl_minutes=10**6)
        if stored is None:
            return {}
        return {
            **correlate.correlation_for(
                points=stored.origin,
                findings=[],
                incidents=[],
                threshold_pct=threshold_pct,
                tolerance_s=settings.correlation_tolerance_s,
                min_minutes=settings.spike_min_minutes,
            ),
            "spike_source": "realtime per-minute",
            "spike_window": {"start": window.start.isoformat(), "end": window.end.isoformat()},
        }

    try:
        minutes = await cascada_service.channel_window(
            service_id=item["service_id"],
            channel_name=item["channel_name"],
            country=item.get("country") or "",
        )
    except Exception as exc:
        return {
            "spike_source": "realtime per-minute",
            "error": f"The realtime per-minute series could not be read: {exc}",
            "windows": [],
            "spike_count": 0,
            "matched_count": 0,
        }

    spike_window = {
        "start": minutes.window.start.isoformat(),
        "end": minutes.window.end.isoformat(),
    }
    base: dict[str, Any] = {
        "spike_source": "realtime per-minute",
        "spike_window": spike_window,
        "aging_job_id": aging_job_id,
        "aging_batch_id": aging_batch_id,
    }

    findings: list[dict[str, Any]] = []
    incidents: list[dict[str, Any]] = []
    if aging_job_id:
        period = await _aging_period(aging_job_id)
        if period is not None:
            base["aging_period"] = {"start": period[0].isoformat(), "end": period[1].isoformat()}
            if period[0] < minutes.window.start:
                # Correlating would compare part of the run against nothing.
                return {
                    **base,
                    "coverage": (
                        f"Not correlated: aging run {aging_job_id} started "
                        f"{period[0]:%Y-%m-%d %H:%M} UTC, before the realtime per-minute window "
                        f"({minutes.window.start:%Y-%m-%d %H:%M} → "
                        f"{minutes.window.end:%Y-%m-%d %H:%M} UTC), so the window does not "
                        "cover the aging period."
                    ),
                    "windows": [],
                    "spike_count": 0,
                    "matched_count": 0,
                }
            base["coverage"] = "The realtime per-minute window covers the whole aging period."
        findings, incidents = await _job_evidence(aging_job_id)

    return {
        **correlate.correlation_for(
            points=minutes.origin,
            findings=findings,
            incidents=incidents,
            threshold_pct=threshold_pct,
            tolerance_s=settings.correlation_tolerance_s,
            min_minutes=settings.spike_min_minutes,
        ),
        **base,
    }


async def build(batch_id: str) -> dict[str, Any]:
    """Write every channel's summary, then render and store the report."""
    current = await store.read(batch_id, with_items=True)
    if current is None:
        raise ValueError(f"batch {batch_id} does not exist")

    settings = BatchSettings.from_snapshot(current["settings"])
    threshold_pct = float((current["settings"] or {}).get("threshold_pct", 0.25))
    window = _window_of(current)
    source = current.get("data_source", "realtime")
    aging_batch_id, previous_aging = await _previous_aging(current)

    for item in current.get("items", []):
        findings, incidents = await _job_evidence(item.get("job_id"))
        # A channel the historical scan could not judge has no spikes to look for.
        judged = item["status"] not in exports.NOT_JUDGED
        aging_job_id = item.get("aging_job_id") or previous_aging.get(item["service_id"])
        correlation = (
            await _correlation(
                item,
                window,
                settings,
                threshold_pct,
                source=source,
                aging_job_id=aging_job_id,
                aging_batch_id=aging_batch_id if not item.get("aging_job_id") else current["id"],
            )
            if window and judged
            else {}
        )
        text = summary.build(
            status=item["status"],
            findings=findings,
            incidents=incidents,
            correlation=correlation,
            error=item.get("error"),
        )
        await store.update_item(batch_id, item["service_id"], summary=text, correlation=correlation)

    # Read back, so the report is rendered from what was written rather than from memory.
    finished = await store.read(batch_id, with_items=True)
    assert finished is not None
    stem = exports.filename(finished["country"], "", None).rstrip(".")

    stored_csv = await service.store_report(
        handle_id=batch_id,
        channel_name=f"Automated batch — {finished['country']}",
        job_type="batch",
        fmt="csv",
        data=exports.csv_bytes(finished),
        stem=f"{stem}-csv",
        verdict_status=finished["status"],
    )
    stored_xlsx = await service.store_report(
        handle_id=batch_id,
        channel_name=f"Automated batch — {finished['country']}",
        job_type="batch",
        fmt="xlsx",
        data=exports.xlsx_bytes(finished),
        stem=stem,
        verdict_status=finished["status"],
    )
    return {"csv": stored_csv, "xlsx": stored_xlsx}


def _window_of(current: dict[str, Any]) -> Window | None:
    start, end = current.get("window_from"), current.get("window_to")
    if not start or not end:
        return None
    return Window(
        start=dt.datetime.fromisoformat(str(start)), end=dt.datetime.fromisoformat(str(end))
    )


def _unsummarised(batch: dict[str, Any]) -> bool:
    """True when the batch finished but its report was never built.

    `build` writes each channel's summary before it renders anything, so a finished batch whose
    analysed channels carry no summary is one whose build failed part way — the sort that ran
    MySQL out of memory did exactly that — and downloading it would print empty cells.
    """
    if batch.get("running"):
        return False
    return any(
        item["status"] in exports.ANALYSED and not item.get("summary")
        for item in batch.get("items", [])
    )


async def render(batch_id: str, fmt: str) -> tuple[bytes, str, str] | None:
    """The report bytes for a batch, rebuilt from its rows, built first if it never was."""
    current = await store.read(batch_id, with_items=True)
    if current is None:
        return None
    if _unsummarised(current):
        await store.log(batch_id, "The report was never built; building it for this download.")
        await build(batch_id)
        current = await store.read(batch_id, with_items=True)
        if current is None:
            return None

    media = {
        "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }[fmt]
    data = exports.csv_bytes(current) if fmt == "csv" else exports.xlsx_bytes(current)
    return data, media, exports.filename(current["country"], fmt)
