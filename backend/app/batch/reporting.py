"""Turning a finished batch into the report, and the summaries that fill it.

This runs after the analyses, and everything it needs is already in the database — the batch,
its channels, the findings each analysis produced, and the CASCADA series each channel was
selected on. That is deliberate: a report can be regenerated at any time without re-running a
single channel, which is what makes a download still work weeks later.

Where a channel was aging through the measured week, the aging run's findings are correlated
against the minutes the rebuffering ratio was high, and the result goes into the summary and
onto the correlation sheet.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import select

from app.api.job_views import finding_payload, incident_payload
from app.batch import correlate, exports, store, summary
from app.batch.settings import BatchSettings
from app.cascada import store as cascada_store
from app.cascada.series import Window
from app.db import session as db_session
from app.db.models import Finding as FindingRow
from app.db.models import Incident as IncidentRow
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


async def _correlation(
    item: dict[str, Any], window: Window, settings: BatchSettings, threshold_pct: float
) -> dict[str, Any]:
    """The spike windows for this channel, and what its aging run captured in them.

    Only a channel that was aging through the measured week has anything to correlate: the
    aging run is the other half of the comparison. Without one there are still spike windows,
    and saying so — rather than omitting them — is what tells a reader the week had spikes
    nobody was watching.
    """
    stored = await cascada_store.read(item["service_id"], window, ttl_minutes=10**6)
    if stored is None:
        return {}

    findings, incidents = await _job_evidence(item.get("aging_job_id"))
    return correlate.correlation_for(
        points=stored.origin,
        findings=findings,
        incidents=incidents,
        threshold_pct=threshold_pct,
        tolerance_s=settings.correlation_tolerance_s,
        min_minutes=settings.spike_min_minutes,
    )


async def build(batch_id: str) -> dict[str, Any]:
    """Write every channel's summary, then render and store the report."""
    current = await store.read(batch_id, with_items=True)
    if current is None:
        raise ValueError(f"batch {batch_id} does not exist")

    settings = BatchSettings.from_snapshot(current["settings"])
    threshold_pct = float((current["settings"] or {}).get("threshold_pct", 0.25))
    window = _window_of(current)

    for item in current.get("items", []):
        findings, incidents = await _job_evidence(item.get("job_id"))
        correlation = await _correlation(item, window, settings, threshold_pct) if window else {}
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


async def render(batch_id: str, fmt: str) -> tuple[bytes, str, str] | None:
    """The report bytes for a batch, rebuilt from its rows if none is stored yet."""
    current = await store.read(batch_id, with_items=True)
    if current is None:
        return None

    media = {
        "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }[fmt]
    data = exports.csv_bytes(current) if fmt == "csv" else exports.xlsx_bytes(current)
    return data, media, exports.filename(current["country"], fmt)
