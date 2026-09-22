"""Report generation and storage.

A report is rendered, stored, and served back by id. Storage is the database by default —
the bytes live in the reports table, so a deployment keeps nothing on local disk and a
report outlives the container that rendered it. Set `RBA_REPORT_STORAGE=both` to mirror a
copy into `RBA_REPORTS_DIR` as well.

Generating a report never interrupts a running job: the Realtime tab can produce one at any
moment from the data collected so far.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import re
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import undefer

from app.analysis.engine import AnalysisResult
from app.config import get_settings
from app.db import session as db_session
from app.db.models import PlaylistSnapshot, Report
from app.jobs.manager import JobHandle
from app.reports.render_html import render_bulk_report, render_html_for_handle
from app.reports.render_pdf import PdfUnavailable, render_pdf_bytes

logger = logging.getLogger(__name__)

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(text: str) -> str:
    return SAFE_NAME.sub("-", text).strip("-").lower()[:64] or "channel"


def reports_dir() -> Path:
    directory = get_settings().rba_reports_dir
    directory.mkdir(parents=True, exist_ok=True)
    return directory


async def store_report(
    *,
    handle_id: str,
    channel_name: str | None,
    job_type: str,
    fmt: str,
    data: bytes,
    stem: str,
    verdict_status: str | None,
    owner: str | None = None,
    risk_score: float | None = None,
) -> dict[str, Any]:
    """Record one rendered report and return the row as the API reports it.

    The bytes go to the database. A copy is written to disk only when the deployment asks
    for it, and the path is recorded so an existing mirror stays readable.
    """
    settings = get_settings()
    path: Path | None = None
    if settings.reports_on_disk:
        path = reports_dir() / f"{stem}.{fmt}"
        path.write_bytes(data)

    async with db_session.session_scope() as session:
        row = Report(
            job_id=handle_id,
            channel_name=channel_name,
            job_type=job_type,
            format=fmt,
            content=data if settings.reports_in_database else None,
            path=str(path) if path else None,
            verdict_status=verdict_status,
            owner=owner,
            risk_score=risk_score,
            size_bytes=len(data),
        )
        session.add(row)
        await session.flush()
        return {
            "id": row.id,
            "format": fmt,
            "path": str(path) if path else None,
            "url": f"/api/reports/{row.id}/download",
            "size_bytes": row.size_bytes,
        }


async def generate(
    handle: JobHandle,
    *,
    fmt: str = "html",
    result: AnalysisResult | None = None,
) -> dict[str, Any]:
    """Render a channel report for one job and record it."""
    analysis = result or handle.result
    if analysis is None:
        raise ValueError("This job has produced no result to report on yet")

    html = render_html_for_handle(handle, analysis)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    stem = f"{_slug(handle.channel_name)}-{handle.type}-{stamp}-{handle.id[:8]}"

    rendered: list[tuple[str, bytes]] = [("html", html.encode("utf-8"))]

    pdf_note: str | None = None
    if fmt == "pdf":
        try:
            rendered.append(("pdf", await render_pdf_bytes(html)))
        except PdfUnavailable as exc:
            pdf_note = str(exc)
            logger.warning("PDF export did not run for %s: %s", handle.id, exc)

    verdict = analysis.verdict
    records = [
        await store_report(
            handle_id=handle.id,
            channel_name=handle.channel_name,
            job_type=handle.type,
            fmt=kind,
            data=data,
            stem=stem,
            verdict_status=verdict.status.value,
            owner=verdict.owner,
            risk_score=float(verdict.risk_score),
        )
        for kind, data in rendered
    ]

    primary = next((r for r in records if r["format"] == fmt), records[0])
    return {**primary, "generated": records, "note": pdf_note}


async def generate_bulk(
    parent: JobHandle,
    children: list[JobHandle],
    *,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """The consolidated report for a bulk job, plus a ZIP carrying every per-channel report."""
    started = parent.started_at or parent.created_at
    ended = parent.finished_at or dt.datetime.now(dt.UTC)

    # Per-channel reports are held in memory: they go into the archive, which is itself
    # stored as a report row rather than left on the host.
    per_channel: list[tuple[str, str, str]] = []
    for child in children:
        if child.result is None:
            continue
        try:
            html = render_html_for_handle(child, child.result)
        except Exception as exc:
            logger.warning("per-channel report failed for %s: %s", child.id, type(exc).__name__)
            continue
        name = f"{_slug(child.channel_name)}-{child.id[:8]}.html"
        per_channel.append((child.channel_name, name, html))

    for row in rows:
        match = next(
            (n for channel, n, _ in per_channel if channel == row.get("channel_name")), None
        )
        row["report_path"] = match

    consolidated_html = render_bulk_report(
        rows, job_id=parent.id, started_at=started, ended_at=ended
    )
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    stem = f"bulk-{stamp}-{parent.id[:8]}"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("consolidated.html", consolidated_html)
        for _channel, name, html in per_channel:
            bundle.writestr(f"channels/{name}", html)
    archive_bytes = buffer.getvalue()

    highest_risk = max((r.get("risk_score") or 0 for r in rows), default=0.0)
    consolidated_record = await store_report(
        handle_id=parent.id,
        channel_name=f"Bulk — {len(rows)} channel(s)",
        job_type="bulk",
        fmt="html",
        data=consolidated_html.encode("utf-8"),
        stem=stem,
        verdict_status="BULK",
        risk_score=float(highest_risk),
    )
    archive_record = await store_report(
        handle_id=parent.id,
        channel_name=f"Bulk — {len(rows)} channel(s)",
        job_type="bulk",
        fmt="zip",
        data=archive_bytes,
        stem=stem,
        verdict_status="BULK",
        risk_score=float(highest_risk),
    )

    return {
        "id": consolidated_record["id"],
        "zip_id": archive_record["id"],
        "consolidated": consolidated_record["path"],
        "zip": archive_record["path"],
        "url": f"/api/reports/{consolidated_record['id']}/download",
        "zip_url": f"/api/bulk/jobs/{parent.id}/reports.zip",
        "channel_reports": len(per_channel),
    }


def _bundle_readme(handle: JobHandle) -> str:
    """What this bundle holds and under what conditions it was collected.

    The User-Agent belongs here for the same reason it belongs in the report appendix: a CDN
    and a packager both answer per User-Agent, so somebody replaying these URLs by hand gets
    a different response unless they send the same string.
    """
    # A bundle can be pulled while the run is still going, so the options come off the handle
    # when no result exists yet. Both carry the same fields.
    options = handle.result.options if handle.result is not None else handle.options.public()
    lines = [
        "RBA evidence bundle",
        "",
        f"Job:       {handle.id}",
        f"Channel:   {handle.channel_name}",
        "",
        "result.json  every finding, measurement and verdict this run produced",
        "playlists/   the manifests as they were served, one file per refresh",
        "",
        "Requests in this run were made with:",
        f"  User-Agent profile  {options.get('ua_profile', 'unknown')}",
        f"  User-Agent          {options.get('user_agent', 'unknown')}",
        "",
        "TLS verification is off on every fetch, by design; the certificate chain is still",
        "inspected and reported in result.json.",
        "",
    ]
    return "\n".join(lines)


async def build_evidence_bundle(handle: JobHandle) -> bytes:
    """Segments and playlist snapshots recorded around each incident, as ZIP bytes.

    Built in memory from the database rows and streamed to the caller, so an evidence
    download leaves nothing behind on the analyzer host.
    """
    async with db_session.session_scope() as session:
        snapshots = (
            (
                await session.execute(
                    select(PlaylistSnapshot)
                    .where(PlaylistSnapshot.job_id == handle.id)
                    .order_by(PlaylistSnapshot.ts)
                )
            )
            .scalars()
            .all()
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        if handle.result is not None:
            bundle.writestr(
                "result.json",
                io.StringIO(
                    __import__("json").dumps(handle.result.as_dict(), indent=2, default=str)
                ).getvalue(),
            )
        bundle.writestr("README.txt", _bundle_readme(handle))
        for snapshot in snapshots:
            stamp = snapshot.ts.strftime("%Y%m%dT%H%M%S%f")
            bundle.writestr(
                f"playlists/{_slug(snapshot.variant)}/{stamp}.m3u8",
                snapshot.raw,
            )
        if handle.session is not None:
            for context in handle.session.layers.values():
                for variant, target in context.targets.items():
                    for snapshot_obj in target.history[-20:]:
                        if not snapshot_obj.raw:
                            continue
                        stamp = snapshot_obj.at.strftime("%Y%m%dT%H%M%S%f")
                        bundle.writestr(
                            f"playlists/{_slug(variant)}/{stamp}.m3u8", snapshot_obj.raw
                        )
    return buffer.getvalue()


async def list_reports(
    *,
    channel: str | None = None,
    owner: str | None = None,
    verdict: str | None = None,
    since: dt.datetime | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    async with db_session.session_scope() as session:
        query = select(Report).order_by(Report.created_at.desc()).limit(limit)
        if channel:
            query = query.where(Report.channel_name.ilike(f"%{channel}%"))
        if owner:
            query = query.where(Report.owner == owner)
        if verdict:
            query = query.where(Report.verdict_status.ilike(f"%{verdict}%"))
        if since:
            query = query.where(Report.created_at >= since)
        rows = (await session.execute(query)).scalars().all()
        return [
            {
                "id": row.id,
                "job_id": row.job_id,
                "channel_name": row.channel_name,
                "job_type": row.job_type,
                "format": row.format,
                "verdict_status": row.verdict_status,
                "owner": row.owner,
                "risk_score": row.risk_score,
                "size_bytes": row.size_bytes,
                "created_at": row.created_at.isoformat(),
                "url": f"/api/reports/{row.id}/download",
                "exists": row.size_bytes > 0,
            }
            for row in rows
        ]


MEDIA_TYPES = {
    "pdf": "application/pdf",
    "zip": "application/zip",
    "html": "text/html; charset=utf-8",
}


async def get_report(report_id: int) -> tuple[bytes, str, str] | None:
    """The stored report as bytes, its media type, and the filename to offer.

    The database holds the report. A row written by an older deployment that kept reports
    on disk still resolves, by reading the path it recorded.
    """
    async with db_session.session_scope() as session:
        # The blob is deferred on the model, so this read asks for it explicitly.
        row = await session.get(Report, report_id, options=[undefer(Report.content)])
        if row is None:
            return None
        data = row.content
        if data is None and row.path:
            path = Path(row.path)
            data = path.read_bytes() if path.exists() else None
        if data is None:
            return None
        media = MEDIA_TYPES.get(row.format, "application/octet-stream")
        return data, media, f"{_slug(row.channel_name or 'report')}.{row.format}"


async def delete_report(report_id: int) -> bool:
    async with db_session.session_scope() as session:
        row = await session.get(Report, report_id)
        if row is None:
            return False
        if row.path:
            Path(row.path).unlink(missing_ok=True)
        await session.execute(delete(Report).where(Report.id == report_id))
        return True
