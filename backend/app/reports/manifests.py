"""The playlists a report shows: each finding's and each incident's, as they were at the time.

A report is read away from the tool — attached to a ticket, forwarded to a CDN — so it has to
carry the evidence itself rather than point at a screen. For every rendition a finding was
measured on, the poll its latest occurrence names is read from the poll log and cut down to the
lines that matter, with the ones at fault marked. For every incident, the poll of each affected
rendition current when it began. Both state their poll number, so the full body is one lookup
away in the network log.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.analysis.engine import AnalysisResult
from app.analysis.rules.base import Finding
from app.hls.locate import locate
from app.reports import network_log

CONTEXT_LINES = 6
MAX_EXCERPT_LINES = 60
HEAD_LINES = 25
MAX_INCIDENT_RENDITIONS = 3


def _excerpt(
    poll: dict[str, Any], *, evidence: dict[str, Any], text: str, exact: bool
) -> dict[str, Any]:
    raw = poll.get("raw") or ""
    lines = raw.splitlines()
    marks = {
        h.line: h.reason
        for h in locate(
            raw,
            evidence=evidence,
            text=text,
            base_url=poll.get("final_url") or poll.get("url") or "",
        )
    }
    if marks:
        start = max(1, min(marks) - CONTEXT_LINES)
        end = min(len(lines), max(marks) + CONTEXT_LINES)
        if end - start + 1 > MAX_EXCERPT_LINES:
            end = start + MAX_EXCERPT_LINES - 1
    else:
        start, end = 1, min(len(lines), HEAD_LINES)
    return {
        "variant": poll.get("variant"),
        "seq": poll.get("seq"),
        "at": poll.get("at"),
        "status": poll.get("status"),
        "url": poll.get("final_url") or poll.get("url"),
        "exact": exact,
        "total_lines": len(lines),
        "hidden_before": start - 1,
        "hidden_after": max(0, len(lines) - end),
        "marked": len(marks),
        "lines": [
            {"n": n, "text": lines[n - 1], "reason": marks.get(n, "")}
            for n in range(start, end + 1)
        ],
    }


def _latest_sample(finding: Finding) -> dict[str, Any]:
    for sample in reversed(finding.evidence):
        if isinstance(sample, dict) and isinstance(sample.get("poll"), dict):
            return sample
    return (
        finding.evidence[-1] if finding.evidence and isinstance(finding.evidence[-1], dict) else {}
    )


async def finding_manifest(job_id: str, finding: Finding) -> dict[str, Any] | None:
    sample = _latest_sample(finding)
    found_ref = sample.get("poll")
    poll_ref: dict[str, Any] = found_ref if isinstance(found_ref, dict) else {}
    seq = poll_ref.get("seq") if isinstance(poll_ref.get("seq"), int) else None
    variant = finding.variant or poll_ref.get("variant")
    poll = await network_log.poll_with_context(
        job_id, seq=seq, variant=variant, at=finding.last_seen
    )
    if poll is None:
        return None
    return _excerpt(
        poll,
        evidence=sample,
        text=f"{finding.rule.title} {finding.detail}",
        exact=seq is not None and poll.get("seq") == seq,
    )


async def incident_manifests(
    job_id: str, incident: Any, renditions: list[str]
) -> list[dict[str, Any]]:
    variants = [incident.variant] if incident.variant else renditions[:MAX_INCIDENT_RENDITIONS]
    started = incident.started_at
    if isinstance(started, str):
        started = dt.datetime.fromisoformat(started)
    found: list[dict[str, Any]] = []
    for variant in variants:
        poll = await network_log.poll_with_context(job_id, variant=variant, at=started)
        if poll is None:
            continue
        detail = incident.detail if isinstance(incident.detail, dict) else {}
        found.append(
            _excerpt(
                poll,
                evidence=detail,
                text=" ".join(incident.cause_chain or [])
                + " "
                + " ".join(incident.cause_rule_ids or []),
                exact=False,
            )
        )
    return found


async def report_manifests(job_id: str, result: AnalysisResult) -> dict[str, Any]:
    """Every manifest excerpt a report carries, keyed for the template."""
    by_finding: dict[str, dict[str, Any]] = {}
    for finding in result.findings:
        if finding.severity.value in ("PASS", "INFO"):
            continue
        excerpt = await finding_manifest(job_id, finding)
        if excerpt is not None:
            by_finding[finding_key(finding)] = excerpt
    renditions = sorted({f.variant for f in result.findings if f.variant and f.variant != "master"})
    by_incident = [
        await incident_manifests(job_id, incident, renditions) for incident in result.incidents
    ]
    return {"findings": by_finding, "incidents": by_incident}


def finding_key(finding: Finding) -> str:
    return f"{finding.rule.id}|{finding.stream_layer.value}|{finding.variant or ''}"
