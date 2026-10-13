"""An escalation pack: everything one owner needs, and nothing that is somebody else's.

A CDN ticket wants the CDN's defects, the playlists they were measured on and a way to
reproduce the requests — not the packager's findings or a hundred-page report. One owner per
pack:

    ESCALATION.txt          the subject and body, ready to paste into a ticket
    findings.json           that owner's findings, one entry per issue with every rendition
    manifests/              the playlist each rendition's latest occurrence was measured on,
                            exactly as served, with <file>.marked.txt naming the lines at fault
    reproduce.sh            a cURL per request the findings measured, as the run's User-Agent
    README.txt              what is here

Built from the run in memory when it is there, otherwise from the database and the poll log,
so a pack can be built for last week's aging run as easily as for a live session.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

from sqlalchemy import select

from app.analysis import review
from app.analysis.grouping import group_findings
from app.analysis.rules import catalogue  # noqa: F401  (registers every rule)
from app.analysis.rules.base import Finding, Owner, Severity, StreamLayer, registry
from app.db import session as db_session
from app.db.models import Finding as FindingRow
from app.db.models import Job as JobRow
from app.hls.locate import locate
from app.jobs.manager import job_manager
from app.net.curl import curl_command
from app.reports import network_log
from app.reports.email_block import build_blocks

TOOL_NAME = "StreamLens"


async def findings_for_job(job_id: str) -> list[Finding]:
    """The run's findings: from memory when the run is there, otherwise rebuilt from the rows."""
    handle = job_manager.get(job_id)
    if handle is not None and handle.result is not None:
        return list(handle.result.findings)
    if handle is not None and handle.session is not None:
        return handle.session.collector.all()
    async with db_session.session_scope() as session:
        rows = (
            (await session.execute(select(FindingRow).where(FindingRow.job_id == job_id)))
            .scalars()
            .all()
        )
    findings: list[Finding] = []
    for row in rows:
        if row.rule_id not in registry:
            continue  # a rule the catalogue no longer declares
        findings.append(
            Finding(
                rule=registry.get(row.rule_id),
                detail=row.detail,
                variant=row.variant,
                stream_layer=StreamLayer(row.stream_layer),
                severity=Severity(row.severity),
                owner=Owner(row.owner),
                count=row.count,
                first_seen=row.first_seen,
                last_seen=row.last_seen,
                evidence=list(row.evidence or []),
                layer_presence=dict(row.layer_presence or {}),
            )
        )
    return findings


def _latest_with_poll(finding: Finding) -> dict[str, Any]:
    for sample in reversed(finding.evidence):
        if isinstance(sample, dict) and isinstance(sample.get("poll"), dict):
            return sample
    return {}


def _request_urls(finding: Finding) -> list[str]:
    urls: list[str] = []
    for sample in reversed(finding.evidence):
        if not isinstance(sample, dict):
            continue
        for key in ("url", "uri"):
            value = sample.get(key)
            if (
                isinstance(value, str)
                and value.startswith(("http://", "https://"))
                and value not in urls
            ):
                urls.append(value)
    return urls


async def build_pack(job_id: str, owner: str) -> bytes | None:
    """The pack for one owner, or None when the run has no actionable finding for them."""
    try:
        wanted = Owner(owner)
    except ValueError:
        return None
    async with db_session.session_scope() as session:
        job = await session.get(JobRow, job_id)
    # A channel's accepted behaviours are not escalated.
    acks = await review.matches_for(job.playback_url if job is not None else None)
    findings = [
        f
        for f in await findings_for_job(job_id)
        if f.owner == wanted
        and f.severity not in (Severity.PASS, Severity.INFO)
        and not review.is_acknowledged(acks, f.rule.id, f.variant)
    ]
    if not findings:
        return None

    channel = (job.channel_name if job is not None else "") or job_id
    playback_url = (job.playback_url if job is not None else "") or ""
    user_agent = await network_log.job_user_agent(job_id)
    started = min(f.first_seen for f in findings)
    ended = max(f.last_seen for f in findings)
    block = build_blocks(
        findings,
        channel_name=channel,
        window_start=started,
        window_end=ended,
        playback_url=playback_url,
        rebuffer_ratio=None,
        threshold=0.0,
        user_agent=user_agent,
    )[0]

    groups = group_findings(findings)
    buffer = io.BytesIO()
    manifests = 0
    commands: list[str] = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as pack:
        pack.writestr("ESCALATION.txt", f"Subject: {block.subject}\n\n{block.body}\n")
        pack.writestr(
            "findings.json",
            json.dumps(
                [
                    {
                        "rule_id": group.rule_id,
                        "title": group.lead.rule.title,
                        "severity": group.severity.value,
                        "stream_layer": group.stream_layer,
                        "renditions": group.rendition_label(),
                        "occurrences": group.count,
                        "first_seen": group.first_seen.isoformat(),
                        "last_seen": group.last_seen.isoformat(),
                        "root_cause": group.lead.rule.root_cause,
                        "fix": group.lead.rule.fix,
                        "members": [member.as_dict() for member in group.members],
                    }
                    for group in groups
                ],
                indent=2,
                default=str,
            ),
        )
        for group in groups:
            for member in group.members:
                sample = _latest_with_poll(member)
                poll_ref = sample.get("poll") or {}
                poll = await network_log.poll_with_context(
                    job_id,
                    seq=poll_ref.get("seq") if isinstance(poll_ref.get("seq"), int) else None,
                    variant=member.variant,
                    at=member.last_seen,
                )
                if poll is not None and poll.get("raw"):
                    rendition = (member.variant or "ladder").replace("/", "_")
                    name = f"manifests/{group.rule_id}_{rendition}_poll{poll['seq']}.m3u8"
                    pack.writestr(name, poll["raw"])
                    marks = locate(
                        poll["raw"],
                        evidence=sample,
                        text=f"{member.rule.title} {member.detail}",
                        base_url=poll.get("final_url") or poll.get("url") or "",
                    )
                    lines = poll["raw"].splitlines()
                    pack.writestr(
                        name.replace(".m3u8", ".marked.txt"),
                        f"{member.rule.id} on {member.variant or 'the ladder'}: "
                        f"poll #{poll['seq']} at {poll['at']}, "
                        f"{poll.get('final_url') or poll['url']}\n\n"
                        + (
                            "\n".join(
                                f"line {m.line}: {lines[m.line - 1]}    ({m.reason})" for m in marks
                            )
                            or "No single line: the finding concerns the window as a whole."
                        )
                        + "\n",
                    )
                    manifests += 1
                for url in _request_urls(member)[:3]:
                    commands.append(f"# {member.rule.id} {member.variant or 'ladder'}")
                    commands.append(curl_command(url, user_agent))
        pack.writestr(
            "reproduce.sh",
            "#!/bin/sh\n# Each request the findings measured, as the run sent it.\n\n"
            + "\n".join(commands)
            + "\n",
        )
        pack.writestr(
            "README.txt",
            "\n".join(
                [
                    f"{TOOL_NAME} escalation pack — {wanted.label}",
                    "",
                    f"Channel     : {channel}",
                    f"Job         : {job_id}",
                    f"User-Agent  : {user_agent or '(not recorded)'}",
                    f"Issues      : {len(groups)}",
                    f"Manifests   : {manifests}",
                    "",
                    "ESCALATION.txt is ready to paste into a ticket. manifests/ holds the playlist",
                    "each rendition's latest occurrence was measured on, exactly as served, and",
                    "its .marked.txt names the lines at fault. reproduce.sh repeats each request.",
                    "",
                ]
            ),
        )
    return buffer.getvalue()
