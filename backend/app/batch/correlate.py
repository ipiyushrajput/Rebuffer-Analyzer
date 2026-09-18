"""Lining aging findings up against the minutes viewers were actually rebuffering.

CASCADA says *when* a channel rebuffered, minute by minute, from real televisions. An aging
run says *what the stream was doing*, continuously, from the analyzer. Neither alone answers
"what went wrong at 03:14 on Tuesday". Together they can — but only as far as the evidence
goes, which is the rule this module is built around: it reports what was captured inside a
spike window, and when nothing was captured it says exactly that rather than reaching for a
cause.

Everything here works in UTC. The two sides come from different machines and different
systems, so a timestamp that arrives in another zone is converted before it is compared, and
a naive one is read as UTC. A tolerance either side of each window absorbs the clock skew
between the aging host and CASCADA's own collection.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from app.cascada.series import Point

SECONDS_PER_MINUTE = 60


def as_utc(moment: dt.datetime) -> dt.datetime:
    """Any timestamp as UTC. A naive one is taken to be UTC already.

    A mistake here is silent and produces confident nonsense: an event an hour away from a
    spike would be reported as inside it. Every comparison in this module goes through here.
    """
    return moment.astimezone(dt.UTC) if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


@dataclass(frozen=True, slots=True)
class Spike:
    """A run of consecutive minutes whose rebuffering ratio was above the threshold."""

    start: dt.datetime
    end: dt.datetime
    peak_pct: float
    minutes: int

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds() + SECONDS_PER_MINUTE

    def covers(self, moment: dt.datetime, tolerance_s: int) -> bool:
        """Whether an event falls inside this window, allowing for clock skew."""
        slack = dt.timedelta(seconds=max(0, tolerance_s))
        # A minute's reading covers the minute it starts, so the window's end extends by one.
        closes = self.end + dt.timedelta(seconds=SECONDS_PER_MINUTE)
        return self.start - slack <= as_utc(moment) <= closes + slack

    def label(self) -> str:
        return f"{self.start.strftime('%Y-%m-%d %H:%M')}–{self.end.strftime('%H:%M')} UTC"

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "peak_pct": self.peak_pct,
            "minutes": self.minutes,
            "label": self.label(),
        }


@dataclass(frozen=True, slots=True)
class Event:
    """One thing the aging run recorded, reduced to what correlating needs."""

    at: dt.datetime
    kind: str
    label: str
    severity: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "kind": self.kind,
            "label": self.label,
            "severity": self.severity,
            "detail": self.detail,
        }


def spike_windows(points: list[Point], threshold_pct: float, min_minutes: int = 1) -> list[Spike]:
    """The windows where the measured ratio stayed above the threshold.

    Consecutive above-threshold minutes are one window, not several: a ten-minute stretch of
    bad delivery is one event to investigate. A minute CASCADA never measured breaks the run
    rather than extending it — a gap is not evidence of anything.
    """
    spikes: list[Spike] = []
    run: list[Point] = []

    def close() -> None:
        if len(run) >= max(1, min_minutes):
            values = [p.value for p in run if p.value is not None]
            spikes.append(
                Spike(
                    start=as_utc(run[0].at),
                    end=as_utc(run[-1].at),
                    peak_pct=max(values) if values else 0.0,
                    minutes=len(run),
                )
            )
        run.clear()

    for point in sorted(points, key=lambda p: as_utc(p.at)):
        if point.value is not None and point.value > threshold_pct:
            run.append(point)
        else:
            close()
    close()
    return spikes


@dataclass(slots=True)
class Match:
    """One spike window and whatever the aging run captured inside it."""

    spike: Spike
    events: list[Event] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return bool(self.events)

    def sentence(self) -> str:
        """What this window is, in one statement, with no cause attached to it."""
        if not self.events:
            return (
                f"Spike at {self.spike.label()}, peak {self.spike.peak_pct:.3f} % over "
                f"{self.spike.minutes} minute(s): no aging event was captured in window."
            )
        counted: dict[str, int] = {}
        for event in self.events:
            counted[event.label] = counted.get(event.label, 0) + 1
        listed = ", ".join(
            f"{label} ×{count}" if count > 1 else label
            for label, count in sorted(counted.items(), key=lambda pair: -pair[1])
        )
        return (
            f"Spike at {self.spike.label()}, peak {self.spike.peak_pct:.3f} % over "
            f"{self.spike.minutes} minute(s): {len(self.events)} aging event(s) in window — "
            f"{listed}."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.spike.as_dict(),
            "matched": self.matched,
            "event_count": len(self.events),
            "events": [event.as_dict() for event in self.events],
            "sentence": self.sentence(),
        }


def match_events(spikes: list[Spike], events: list[Event], tolerance_s: int) -> list[Match]:
    """Put each aging event into every spike window it falls inside.

    An event can land in two windows when they are close together and the tolerance overlaps
    them; it is reported in both, because it is evidence for both.
    """
    return [
        Match(spike=spike, events=[e for e in events if spike.covers(e.at, tolerance_s)])
        for spike in spikes
    ]


def events_from(findings: list[dict[str, Any]], incidents: list[dict[str, Any]]) -> list[Event]:
    """What an aging run recorded, as events with a time on them.

    A finding is timestamped at the moment it was first seen; an incident at the moment it
    opened. Anything without a usable timestamp is left out — it cannot be placed on a
    timeline, and guessing where it belongs would be inventing evidence.
    """
    events: list[Event] = []

    for finding in findings:
        at = _parse(finding.get("first_seen") or finding.get("at"))
        if at is None:
            continue
        events.append(
            Event(
                at=at,
                kind="finding",
                label=f"{finding.get('rule_id', '')} {finding.get('title', '')}".strip(),
                severity=str(finding.get("severity", "")),
                detail=str(finding.get("detail", ""))[:500],
            )
        )

    for incident in incidents:
        at = _parse(incident.get("started_at"))
        if at is None:
            continue
        events.append(
            Event(
                at=at,
                kind="incident",
                label=str(incident.get("kind", "incident")),
                severity="",
                detail=f"lasted {incident.get('duration_s', 0)} s",
            )
        )

    events.sort(key=lambda event: event.at)
    return events


def _parse(raw: Any) -> dt.datetime | None:
    if isinstance(raw, dt.datetime):
        return as_utc(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return as_utc(dt.datetime.fromisoformat(raw.strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def correlation_for(
    *,
    points: list[Point],
    findings: list[dict[str, Any]],
    incidents: list[dict[str, Any]],
    threshold_pct: float,
    tolerance_s: int,
    min_minutes: int = 1,
) -> dict[str, Any]:
    """One channel's spike windows and what the aging run captured in each."""
    spikes = spike_windows(points, threshold_pct, min_minutes)
    events = events_from(findings, incidents)
    matches = match_events(spikes, events, tolerance_s)
    return {
        "threshold_pct": threshold_pct,
        "tolerance_s": tolerance_s,
        "spike_count": len(spikes),
        "matched_count": sum(1 for match in matches if match.matched),
        "aging_event_count": len(events),
        "windows": [match.as_dict() for match in matches],
    }
