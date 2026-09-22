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
import json
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
from app.drm import settings as drm_settings
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


def _bundle_readme(handle: JobHandle, *, decrypt_evidence: bool) -> str:
    """What this bundle holds, under what conditions it was collected, and what is missing.

    The User-Agent belongs here for the same reason it belongs in the report appendix: a CDN
    and a packager both answer per User-Agent, so somebody replaying these URLs by hand gets
    a different response unless they send the same string.

    A bundle that is short of something says so, and says why. "No decrypted media" has four
    causes — nothing was protected, decryption is switched off in Settings, the key server
    refused, or the scheme needs a binary this host does not have — and a reader who cannot
    tell them apart cannot act on any of them.
    """
    # A bundle can be pulled while the run is still going, so the options come off the handle
    # when no result exists yet. Both carry the same fields.
    options = handle.result.options if handle.result is not None else handle.options.public()
    store = handle.session.evidence if handle.session is not None else None

    lines = [
        "RBA evidence bundle",
        "",
        f"Job:       {handle.id}",
        f"Channel:   {handle.channel_name}",
        "",
        "result.json   every finding, measurement and verdict this run produced",
        "playlists/    the manifests as they were served, one file per refresh",
    ]

    if store is None:
        lines += [
            "",
            "No segment media is included: this run was not asked to record evidence.",
            "Start it with evidence recording on, in the analysis form or in Settings.",
        ]
    else:
        clear = store.count - store.encrypted_count
        if clear:
            lines.append(f"segments/     {clear} segment(s) as served, unprotected")
        if store.encrypted_count:
            lines.append(f"encrypted/    {store.encrypted_count} segment(s) as served, protected")
        if store.decrypted_count and decrypt_evidence:
            lines.append(
                f"decrypted/    {store.decrypted_count} segment(s) decrypted, each with its "
                "initialisation segment in front of it, plus manifest.json mapping every "
                "file to its source URI, media sequence number and rendition"
            )
        lines += ["", f"Segments held: {store.count}."]
        if store.evicted:
            lines.append(
                f"{store.evicted} older segment(s) were dropped to stay inside the evidence "
                "budget; segments sampled while an incident was open are kept longest. Raise "
                "evidence_max_bytes or evidence_segments_per_rendition in Settings to keep more."
            )
        if store.encrypted_count and not decrypt_evidence:
            lines.append(
                "No decrypted media: decryption of evidence is switched off in Settings → DRM."
            )
        elif store.encrypted_count and not store.decrypted_count:
            lines.append("No decrypted media: no segment in this bundle was decrypted.")
        for reason, count in sorted(store.reasons().items()):
            lines.append(f"{count} segment(s) were not decrypted. {reason}")

    lines += [
        "",
        "Requests in this run were made with:",
        f"  User-Agent profile  {options.get('ua_profile', 'unknown')}",
        f"  User-Agent          {options.get('user_agent', 'unknown')}",
        "",
        "TLS verification is off on every fetch, by design; the certificate chain is still",
        "inspected and reported in result.json.",
        "",
        "No content key, private key or CPIX credential is in this bundle. Keys live in",
        "memory for the life of the job and are never written anywhere.",
        "",
    ]
    return "\n".join(lines)


def _write_segments(bundle: zipfile.ZipFile, handle: JobHandle, *, decrypt_evidence: bool) -> None:
    """The sampled media, and a manifest for whatever was decrypted.

    A clear segment lands under `segments/`, a protected one under `encrypted/` as it was
    served, and — when the setting allows it and a key was held — under `decrypted/video/` or
    `decrypted/audio/` with its initialisation segment in front, which is a file a decoder
    opens rather than a fragment it cannot. `decrypted/manifest.json` maps every decrypted
    file back to its source URI, media sequence number and rendition, so a packager can tell
    which segment of which rung reproduces a defect.
    """
    store = handle.session.evidence if handle.session is not None else None
    if store is None or not store.segments:
        return

    manifest: list[dict[str, Any]] = []
    for segment in store.segments:
        folder = "encrypted" if segment.encrypted else "segments"
        path = f"{folder}/{_slug(segment.variant)}/{segment.msn:012d}.{segment.extension}"
        bundle.writestr(path, segment.raw)
        if not decrypt_evidence or not segment.decrypted:
            continue
        # `kind` is the rendition's own — "video", "audio" or "subtitles" — so a reader finds
        # the track they are chasing without opening every file.
        decrypted_path = (
            f"decrypted/{segment.kind or 'video'}/{_slug(segment.variant)}/{segment.msn:012d}.mp4"
        )
        bundle.writestr(decrypted_path, segment.decrypted)
        manifest.append(segment.manifest_row(decrypted_path))

    if manifest:
        bundle.writestr("decrypted/manifest.json", json.dumps(manifest, indent=2, default=str))


async def build_evidence_bundle(handle: JobHandle) -> bytes:
    """Segments and playlist snapshots recorded during the run, as ZIP bytes.

    Built in memory from the database rows and the session's own evidence store, and streamed
    to the caller, so an evidence download leaves nothing behind on the analyzer host.

    The segments are the part that was missing: this function wrote `result.json` and the
    playlists and claimed in its docstring to write segments, and wrote none — for a clear
    channel as much as a protected one. A protected segment goes in twice when the setting
    allows it, as served and as decrypted, because the two answer different questions: what
    the CDN sent, and what a decoder reads.
    """
    drm = await drm_settings.load()
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
                json.dumps(handle.result.as_dict(), indent=2, default=str),
            )
        bundle.writestr("README.txt", _bundle_readme(handle, decrypt_evidence=drm.decrypt_evidence))
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
            _write_segments(bundle, handle, decrypt_evidence=drm.decrypt_evidence)
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
