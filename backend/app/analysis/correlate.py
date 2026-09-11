"""Stall-to-cause correlation and incident hysteresis.

Every stall — real player stall or one the Virtual Player Buffer produced — is matched to
the backend events measured in the window `[stall_start − 2 × TARGETDURATION, stall_end]` on
the rung being played. The result is a chain the report prints verbatim, for example:

    CDN stale playlist 720p 12:03:02–12:03:20 (MED-004) -> segment 48213 listed late
    -> player stall 12:03:14, 4.1 s
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from app.analysis.rules.base import Finding, Severity
from app.config import Thresholds

# Findings that can cause a stall, ranked by how directly they do so.
CAUSE_PRIORITY = {
    "MED-004": 100,  # stale playlist
    "HTTP-005": 98,  # 404 on a listed segment
    "SEG-018": 96,  # segment download failure
    "HTTP-008": 95,  # 5xx
    "SEG-017": 90,  # slow segment download
    "CDN-006": 88,  # download ratio breach
    "HTTP-011": 86,  # request timeout
    "CDN-008": 84,  # wrong media sequence
    "CDN-002": 82,  # stale edge object
    "CDN-003": 82,  # CDN behind origin
    "SEQ-001": 78,
    "SEQ-003": 78,
    "MED-012": 76,  # EXT-X-GAP
    "SEG-008": 72,  # PTS gap
    "SEG-010": 72,  # PTS reset
    "AUD-006": 70,  # missing audio segment
    "SEQ-009": 68,  # cross-variant DSN mismatch
    "VID-001": 66,  # non-keyframe start
    "CDN-007": 60,  # throughput below declared
    "MST-007": 55,  # declared bandwidth understated
}

DEFAULT_PRIORITY = 10


@dataclass(slots=True)
class StallEvent:
    """A stall from the player or from the Virtual Player Buffer."""

    started_at: dt.datetime
    ended_at: dt.datetime | None
    variant: str
    source: str  # "player" | "vpb"
    duration_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "variant": self.variant,
            "source": self.source,
            "duration_s": self.duration_s,
        }


@dataclass(slots=True)
class CausalChain:
    stall: StallEvent
    causes: list[Finding] = field(default_factory=list)

    @property
    def primary(self) -> Finding | None:
        return self.causes[0] if self.causes else None

    def describe(self) -> str:
        """One deterministic sentence naming the chain."""
        end = self.stall.ended_at.strftime("%H:%M:%S") if self.stall.ended_at else "ongoing"
        tail = (
            f"{self.stall.source} stall {self.stall.started_at.strftime('%H:%M:%S')}"
            f"–{end}, {self.stall.duration_s:.1f} s on {self.stall.variant}"
        )
        if not self.causes:
            return (
                f"{tail}. No stream-side event was measured in the correlation window for this "
                "stall."
            )
        steps = []
        for finding in self.causes[:3]:
            moment = finding.first_seen.strftime("%H:%M:%S")
            where = finding.variant or "the channel"
            steps.append(f"{finding.rule.title} on {where} at {moment} ({finding.rule.id})")
        return " -> ".join(steps) + f" -> {tail}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "stall": self.stall.as_dict(),
            "chain": self.describe(),
            "causes": [
                {
                    "rule_id": f.rule.id,
                    "title": f.rule.title,
                    "severity": f.severity.value,
                    "owner": f.owner.value,
                    "variant": f.variant,
                    "at": f.first_seen.isoformat(),
                    "detail": f.detail,
                }
                for f in self.causes[:5]
            ],
        }


def correlate(
    stalls: list[StallEvent],
    findings: list[Finding],
    *,
    target_duration: float,
    thresholds: Thresholds,
) -> list[CausalChain]:
    """Match every stall to the events that explain it."""
    chains: list[CausalChain] = []
    lookback = dt.timedelta(seconds=max(target_duration, 1.0) * 2)

    for stall in stalls:
        window_start = stall.started_at - lookback
        window_end = stall.ended_at or stall.started_at
        candidates: list[tuple[float, Finding]] = []

        for finding in findings:
            if finding.severity in (Severity.PASS, Severity.INFO):
                continue
            if finding.rule.rebuffer_impact.value == "none":
                continue
            if finding.variant and stall.variant and finding.variant != stall.variant:
                # A ladder-wide finding carries no variant and applies to every rung.
                continue
            overlaps = finding.first_seen <= window_end and finding.last_seen >= window_start
            if not overlaps:
                continue
            score = (
                CAUSE_PRIORITY.get(finding.rule.id, DEFAULT_PRIORITY)
                + finding.severity.rank * 5
                + finding.rule.rebuffer_impact.weight * 10
                + (5 if finding.variant == stall.variant else 0)
            )
            candidates.append((score, finding))

        candidates.sort(key=lambda item: (-item[0], item[1].rule.id))
        chains.append(CausalChain(stall=stall, causes=[f for _, f in candidates]))

    del thresholds
    return chains


@dataclass(slots=True)
class Incident:
    """A condition that persisted long enough to matter, with hysteresis on both edges."""

    kind: str
    variant: str | None
    started_at: dt.datetime
    ended_at: dt.datetime | None = None
    cause_rule_ids: list[str] = field(default_factory=list)
    cause_chain: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        if self.ended_at is None:
            return 0.0
        return (self.ended_at - self.started_at).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "variant": self.variant,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_s": self.duration_s,
            "cause_rule_ids": self.cause_rule_ids,
            "cause_chain": self.cause_chain,
            "detail": self.detail,
        }


class IncidentTracker:
    """Opens an incident only after `incident_open_s` and closes after `incident_clear_s`.

    Without this, one slow segment produces an incident; with it, a report lists the periods
    a viewer experienced rather than raw event spam.
    """

    def __init__(self, thresholds: Thresholds) -> None:
        self.thresholds = thresholds
        self.incidents: list[Incident] = []
        self._pending: dict[tuple[str, str | None], dt.datetime] = {}
        self._open: dict[tuple[str, str | None], Incident] = {}
        self._clearing: dict[tuple[str, str | None], dt.datetime] = {}

    def observe(
        self,
        *,
        kind: str,
        variant: str | None,
        at: dt.datetime,
        degraded: bool,
        cause_rule_ids: list[str] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Incident | None:
        key = (kind, variant)
        if degraded:
            self._clearing.pop(key, None)
            if key in self._open:
                incident = self._open[key]
                for rule_id in cause_rule_ids or []:
                    if rule_id not in incident.cause_rule_ids:
                        incident.cause_rule_ids.append(rule_id)
                return None
            first_seen = self._pending.setdefault(key, at)
            if (at - first_seen).total_seconds() >= self.thresholds.incident_open_s:
                incident = Incident(
                    kind=kind,
                    variant=variant,
                    started_at=first_seen,
                    cause_rule_ids=list(cause_rule_ids or []),
                    detail=detail or {},
                )
                self._open[key] = incident
                self.incidents.append(incident)
                self._pending.pop(key, None)
                return incident
            return None

        self._pending.pop(key, None)
        if key not in self._open:
            return None
        clean_since = self._clearing.setdefault(key, at)
        if (at - clean_since).total_seconds() >= self.thresholds.incident_clear_s:
            incident = self._open.pop(key)
            incident.ended_at = clean_since
            self._clearing.pop(key, None)
            return incident
        return None

    def close_all(self, at: dt.datetime) -> None:
        for key, incident in list(self._open.items()):
            incident.ended_at = at
            self._open.pop(key)
        self._pending.clear()
        self._clearing.clear()

    def attach_chains(self, chains: list[CausalChain]) -> None:
        """Give every incident the causal chains of the stalls inside it."""
        for incident in self.incidents:
            end = incident.ended_at or incident.started_at
            for chain in chains:
                inside = incident.started_at <= chain.stall.started_at <= end
                if inside and incident.variant in (None, chain.stall.variant):
                    incident.cause_chain.append(chain.describe())
                    for finding in chain.causes[:3]:
                        if finding.rule.id not in incident.cause_rule_ids:
                            incident.cause_rule_ids.append(finding.rule.id)


def total_incident_time(incidents: list[Incident]) -> float:
    return sum(incident.duration_s for incident in incidents)
