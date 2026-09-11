"""Verdict: the primary root cause, the contributing defects, and the risk score.

Ranking is `severity × rebuffer impact × occurrence × correlation to stalls`. The formula is
printed in the report appendix so the number can be checked against the findings table.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.analysis.correlate import CausalChain, Incident, total_incident_time
from app.analysis.rules import catalogue as R
from app.analysis.rules.base import Finding, Severity
from app.config import Thresholds

RISK_FORMULA = (
    "risk = min(100, round(100 * (0.45*S + 0.25*I + 0.20*R + 0.10*O))) where "
    "S = worst severity as a fraction of CRITICAL (CRITICAL 1.00, ERROR 0.70, WARN 0.35, "
    "otherwise 0), "
    "I = the highest measured rebuffering ratio divided by the configured threshold, capped "
    "at 1, "
    "R = total incident seconds divided by the analysis window, capped at 1, and "
    "O = log10(1 + the number of direct-impact finding occurrences) / 2, capped at 1."
)

SEVERITY_SCORE = {
    Severity.CRITICAL: 1.0,
    Severity.ERROR: 0.7,
    Severity.WARN: 0.35,
    Severity.INFO: 0.0,
    Severity.PASS: 0.0,
}


class VerdictStatus(str, Enum):
    REBUFFERING_STREAM_DEFECT = "REBUFFERING — STREAM DEFECT"
    REBUFFER_RISK_STREAM_DEFECT = "REBUFFERING RISK — STREAM DEFECT"
    NO_STREAM_SIDE_DEFECT = "NO STREAM-SIDE DEFECT"


# The exact sentence §5.4 requires when the stream side is clean.
def clean_verdict_sentence(duration: str, playlists: int, segments: int) -> str:
    return (
        f"No stream-side defect detected in {duration} across {playlists} playlists and "
        f"{segments} segments. Rebuffering on this channel is not caused by the stream as "
        "delivered to the analyzer. Escalate to Samsung player/device investigation with this "
        "report attached."
    )


@dataclass(slots=True)
class RankedFinding:
    finding: Finding
    score: float
    stall_correlations: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.finding.as_dict(),
            "rank_score": round(self.score, 3),
            "stall_correlations": self.stall_correlations,
        }


@dataclass(slots=True)
class Verdict:
    status: VerdictStatus
    headline: str
    primary: RankedFinding | None
    contributing: list[RankedFinding] = field(default_factory=list)
    risk_score: int = 0
    owner: str | None = None
    owner_label: str | None = None
    required_fix: str | None = None
    measured_rebuffer_ratio: float | None = None
    worst_variant: str | None = None
    incident_count: int = 0
    incident_seconds: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)
    window_seconds: float = 0.0
    playlists_checked: int = 0
    segments_checked: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "headline": self.headline,
            "primary": self.primary.as_dict() if self.primary else None,
            "contributing": [r.as_dict() for r in self.contributing],
            "risk_score": self.risk_score,
            "owner": self.owner,
            "owner_label": self.owner_label,
            "required_fix": self.required_fix,
            "measured_rebuffer_ratio": self.measured_rebuffer_ratio,
            "worst_variant": self.worst_variant,
            "incident_count": self.incident_count,
            "incident_seconds": self.incident_seconds,
            "counts": self.counts,
            "window_seconds": self.window_seconds,
            "playlists_checked": self.playlists_checked,
            "segments_checked": self.segments_checked,
            "risk_formula": RISK_FORMULA,
        }


def _format_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} minutes"
    return f"{minutes / 60:.1f} hours"


def rank(findings: list[Finding], chains: list[CausalChain]) -> list[RankedFinding]:
    """Score every actionable finding by severity, impact, occurrence and correlation."""
    correlation_counts: dict[str, int] = {}
    for chain in chains:
        for position, finding in enumerate(chain.causes[:3]):
            weight = 3 - position
            correlation_counts[finding.rule.id] = (
                correlation_counts.get(finding.rule.id, 0) + weight
            )

    ranked: list[RankedFinding] = []
    for finding in findings:
        if finding.severity in (Severity.PASS, Severity.INFO):
            continue
        correlations = correlation_counts.get(finding.rule.id, 0)
        score = (
            SEVERITY_SCORE[finding.severity]
            * finding.rule.rebuffer_impact.weight
            * (1 + math.log10(1 + finding.count))
            * (1 + correlations * 0.5)
        )
        ranked.append(RankedFinding(finding=finding, score=score, stall_correlations=correlations))

    ranked.sort(key=lambda r: (-r.score, -r.finding.severity.rank, r.finding.rule.id))
    return ranked


def risk_score(
    *,
    worst_severity: Severity,
    measured_ratio: float | None,
    incident_seconds: float,
    window_seconds: float,
    direct_occurrences: int,
    thresholds: Thresholds,
) -> int:
    severity_component = SEVERITY_SCORE[worst_severity]
    ratio_component = 0.0
    if measured_ratio is not None and thresholds.rebuffer_ratio_threshold > 0:
        ratio_component = min(1.0, measured_ratio / thresholds.rebuffer_ratio_threshold)
    incident_component = min(1.0, incident_seconds / window_seconds) if window_seconds > 0 else 0.0
    occurrence_component = min(1.0, math.log10(1 + direct_occurrences) / 2)

    raw = (
        0.45 * severity_component
        + 0.25 * ratio_component
        + 0.20 * incident_component
        + 0.10 * occurrence_component
    )
    return min(100, round(100 * raw))


def build(
    *,
    findings: list[Finding],
    chains: list[CausalChain],
    incidents: list[Incident],
    thresholds: Thresholds,
    window_seconds: float,
    playlists_checked: int,
    segments_checked: int,
    measured_ratio: float | None = None,
    worst_variant: str | None = None,
    started_at: dt.datetime | None = None,
) -> Verdict:
    """Produce the single verdict the report leads with."""
    ranked = rank(findings, chains)
    counts = {severity.value: 0 for severity in Severity}
    for finding in findings:
        counts[finding.severity.value] += 1

    actionable = [f for f in findings if f.severity not in (Severity.PASS, Severity.INFO)]
    worst_severity = (
        max((f.severity for f in actionable), key=lambda s: s.rank) if actionable else Severity.PASS
    )
    direct_occurrences = sum(
        f.count for f in actionable if f.rule.rebuffer_impact.value == "direct"
    )
    incident_seconds = total_incident_time(incidents)

    score = risk_score(
        worst_severity=worst_severity,
        measured_ratio=measured_ratio,
        incident_seconds=incident_seconds,
        window_seconds=window_seconds,
        direct_occurrences=direct_occurrences,
        thresholds=thresholds,
    )

    over_threshold = (
        measured_ratio is not None and measured_ratio > thresholds.rebuffer_ratio_threshold
    )

    if not ranked:
        headline = clean_verdict_sentence(
            _format_duration(window_seconds), playlists_checked, segments_checked
        )
        return Verdict(
            status=VerdictStatus.NO_STREAM_SIDE_DEFECT,
            headline=headline,
            primary=None,
            contributing=[],
            risk_score=score,
            owner=R.NO_DEFECT.owner.value,
            owner_label=R.NO_DEFECT.owner.label,
            required_fix=R.NO_DEFECT.fix,
            measured_rebuffer_ratio=measured_ratio,
            worst_variant=worst_variant,
            incident_count=len(incidents),
            incident_seconds=incident_seconds,
            counts=counts,
            window_seconds=window_seconds,
            playlists_checked=playlists_checked,
            segments_checked=segments_checked,
        )

    primary = ranked[0]
    status = (
        VerdictStatus.REBUFFERING_STREAM_DEFECT
        if over_threshold or primary.stall_correlations > 0
        else VerdictStatus.REBUFFER_RISK_STREAM_DEFECT
    )

    headline = f"{primary.finding.rule.root_cause} {primary.finding.detail}"
    del started_at

    return Verdict(
        status=status,
        headline=headline,
        primary=primary,
        contributing=ranked[1:11],
        risk_score=score,
        owner=primary.finding.owner.value,
        owner_label=primary.finding.owner.label,
        required_fix=primary.finding.rule.fix,
        measured_rebuffer_ratio=measured_ratio,
        worst_variant=worst_variant,
        incident_count=len(incidents),
        incident_seconds=incident_seconds,
        counts=counts,
        window_seconds=window_seconds,
        playlists_checked=playlists_checked,
        segments_checked=segments_checked,
    )
