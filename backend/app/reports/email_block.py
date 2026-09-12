"""Escalation blocks.

One ready-to-send subject and body per responsible party, carrying the defect, the exact
evidence, the impact and the fix. The recipient can forward it without editing.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from app.analysis.rules.base import Finding, Owner, Severity

MAX_FINDINGS_PER_BLOCK = 8
MAX_EVIDENCE_LINES = 3


@dataclass(slots=True)
class EscalationBlock:
    owner: str
    owner_label: str
    subject: str
    body: str
    finding_count: int
    worst_severity: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "owner_label": self.owner_label,
            "subject": self.subject,
            "body": self.body,
            "finding_count": self.finding_count,
            "worst_severity": self.worst_severity,
        }


def _evidence_lines(finding: Finding) -> list[str]:
    lines: list[str] = []
    for sample in finding.evidence[:MAX_EVIDENCE_LINES]:
        parts: list[str] = []
        for key in ("url", "uri", "variant", "msn", "status", "at", "line", "raw_line"):
            value = sample.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={value}")
        for key, value in sample.items():
            if key in ("headers", "cdn_headers") and isinstance(value, dict) and value:
                parts.append(
                    "headers=" + ", ".join(f"{k}: {v}" for k, v in list(value.items())[:4])
                )
        if parts:
            lines.append("      - " + "; ".join(parts))
    return lines


def build_blocks(
    findings: list[Finding],
    *,
    channel_name: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
    playback_url: str,
    rebuffer_ratio: float | None,
    threshold: float,
) -> list[EscalationBlock]:
    """One block per owner that holds at least one actionable finding."""
    by_owner: dict[Owner, list[Finding]] = {}
    for finding in findings:
        if finding.severity in (Severity.PASS, Severity.INFO):
            continue
        by_owner.setdefault(finding.owner, []).append(finding)

    blocks: list[EscalationBlock] = []
    for owner, owned in by_owner.items():
        owned.sort(key=lambda f: (-f.severity.rank, -f.count, f.rule.id))
        worst = owned[0].severity

        subject = (
            f"[TV Plus] {worst.value} — {channel_name} — {owned[0].rule.title} "
            f"({len(owned)} finding(s))"
        )

        body_lines = [
            f"Channel: {channel_name}",
            f"Playback URL: {playback_url}",
            f"Analysis window: {window_start.isoformat()} to {window_end.isoformat()} "
            f"({(window_end - window_start).total_seconds():.0f} s)",
        ]
        if rebuffer_ratio is not None:
            body_lines.append(
                f"Measured rebuffering ratio: {rebuffer_ratio:.3f} against a threshold of "
                f"{threshold}."
            )
        body_lines += [
            "",
            f"The following {len(owned)} defect(s) are owned by {owner.label}.",
            "",
        ]

        for index, finding in enumerate(owned[:MAX_FINDINGS_PER_BLOCK], start=1):
            body_lines += [
                f"{index}. [{finding.severity.value}] {finding.rule.id} — {finding.rule.title}",
                f"   Rendition: {finding.variant or 'the whole ladder'}",
                f"   Occurrences: {finding.count}, first {finding.first_seen.isoformat()}, "
                f"last {finding.last_seen.isoformat()}",
                f"   Measured: {finding.detail}",
                f"   Root cause: {finding.rule.root_cause}",
                f"   Required fix: {finding.rule.fix}",
            ]
            evidence = _evidence_lines(finding)
            if evidence:
                body_lines.append("   Evidence:")
                body_lines += evidence
            body_lines.append("")

        if len(owned) > MAX_FINDINGS_PER_BLOCK:
            body_lines.append(
                f"A further {len(owned) - MAX_FINDINGS_PER_BLOCK} finding(s) for this owner are "
                "listed in the attached report."
            )
            body_lines.append("")

        body_lines.append(
            "The attached report carries the full evidence, the incident timeline and the raw "
            "manifest snapshots around each incident."
        )

        blocks.append(
            EscalationBlock(
                owner=owner.value,
                owner_label=owner.label,
                subject=subject,
                body="\n".join(body_lines),
                finding_count=len(owned),
                worst_severity=worst.value,
            )
        )

    blocks.sort(key=lambda block: (-Severity(block.worst_severity).rank, -block.finding_count))
    return blocks
