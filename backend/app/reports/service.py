"""Report generation and storage.

A report is written to disk, recorded in the database, and served back by id. Generating one
never interrupts a running job: the Realtime tab can produce a report at any moment from the
data collected so far.
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

from app.analysis.engine import AnalysisResult
from app.config import get_settings
from app.db import session as db_session
from app.db.models import PlaylistSnapshot, Report
from app.jobs.manager import JobHandle
from app.reports.render_html import render_bulk_report, render_html_for_handle
from app.reports.render_pdf import PdfUnavailable, render_pdf

logger = logging.getLogger(__name__)

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(text: str) -> str:
    return SAFE_NAME.sub("-", text).strip("-").lower()[:64] or "channel"


def reports_dir() -> Path:
    directory = get_settings().rba_reports_dir
    directory.mkdir(parents=True, exist_ok=True)
    return directory


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

    html_path = reports_dir() / f"{stem}.html"
    html_path.write_text(html, encoding="utf-8")
    written: list[tuple[str, Path]] = [("html", html_path)]

    pdf_note: str | None = None
    if fmt == "pdf":
        try:
            pdf_path = await render_pdf(html, reports_dir() / f"{stem}.pdf")
            written.append(("pdf", pdf_path))
        except PdfUnavailable as exc:
            pdf_note = str(exc)
            logger.warning("PDF export did not run for %s: %s", handle.id, exc)

    verdict = analysis.verdict
    records: list[dict[str, Any]] = []
    async with db_session.session_scope() as session:
        for kind, path in written:
            row = Report(
                job_id=handle.id,
                channel_name=handle.channel_name,
                job_type=handle.type,
                format=kind,
                path=str(path),
                verdict_status=verdict.status.value,
                owner=verdict.owner,
                risk_score=float(verdict.risk_score),
                size_bytes=path.stat().st_size,
            )
            session.add(row)
            await session.flush()
            records.append(
                {
                    "id": row.id,
                    "format": kind,
                    "path": str(path),
                    "url": f"/api/reports/{row.id}/download",
                    "size_bytes": row.size_bytes,
                }
            )

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

    per_channel: list[tuple[str, Path]] = []
    for child in children:
        if child.result is None:
            continue
        try:
            html = render_html_for_handle(child, child.result)
        except Exception as exc:
            logger.warning("per-channel report failed for %s: %s", child.id, type(exc).__name__)
            continue
        path = reports_dir() / f"{_slug(child.channel_name)}-{child.id[:8]}.html"
        path.write_text(html, encoding="utf-8")
        per_channel.append((child.channel_name, path))

    for row in rows:
        match = next((p for name, p in per_channel if name == row.get("channel_name")), None)
        row["report_path"] = match.name if match else None

    consolidated_html = render_bulk_report(
        rows, job_id=parent.id, started_at=started, ended_at=ended
    )
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    consolidated = reports_dir() / f"bulk-{stamp}-{parent.id[:8]}.html"
    consolidated.write_text(consolidated_html, encoding="utf-8")

    archive = reports_dir() / f"bulk-{stamp}-{parent.id[:8]}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("consolidated.html", consolidated_html)
        for _name, path in per_channel:
            bundle.write(path, arcname=f"channels/{path.name}")

    async with db_session.session_scope() as session:
        row = Report(
            job_id=parent.id,
            channel_name=f"Bulk — {len(rows)} channel(s)",
            job_type="bulk",
            format="html",
            path=str(consolidated),
            verdict_status="BULK",
            risk_score=max((r.get("risk_score") or 0 for r in rows), default=0.0),
            size_bytes=consolidated.stat().st_size,
        )
        session.add(row)
        await session.flush()
        report_id = row.id

    return {
        "id": report_id,
        "consolidated": str(consolidated),
        "zip": str(archive),
        "url": f"/api/reports/{report_id}/download",
        "zip_url": f"/api/bulk/jobs/{parent.id}/reports.zip",
        "channel_reports": len(per_channel),
    }


async def build_evidence_bundle(handle: JobHandle) -> Path:
    """Segments and playlist snapshots recorded around each incident, as a ZIP."""
    settings = get_settings()
    directory = settings.rba_evidence_dir
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / f"evidence-{handle.id[:8]}.zip"

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

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        if handle.result is not None:
            bundle.writestr(
                "result.json",
                io.StringIO(
                    __import__("json").dumps(handle.result.as_dict(), indent=2, default=str)
                ).getvalue(),
            )
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
    return archive


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
                "exists": Path(row.path).exists(),
            }
            for row in rows
        ]


async def get_report(report_id: int) -> tuple[Path, str, str] | None:
    async with db_session.session_scope() as session:
        row = await session.get(Report, report_id)
        if row is None:
            return None
        path = Path(row.path)
        media = "application/pdf" if row.format == "pdf" else "text/html; charset=utf-8"
        return path, media, f"{_slug(row.channel_name or 'report')}.{row.format}"


async def delete_report(report_id: int) -> bool:
    async with db_session.session_scope() as session:
        row = await session.get(Report, report_id)
        if row is None:
            return False
        Path(row.path).unlink(missing_ok=True)
        await session.execute(delete(Report).where(Report.id == report_id))
        return True
