"""Virtual Player Buffer.

Models a Tizen-like player against the delivery timings actually measured, so Aging and
Bulk produce a rebuffering ratio with no player attached, and Realtime can show the model
next to the real hls.js buffer.

The model, combining the HLSAnalyzer and Qosifire mechanisms:

* Playback starts once `vpb_startup_buffer_td_multiple × TARGETDURATION` of media has been
  fully downloaded.
* A segment adds its EXTINF to the buffer at the wall-clock instant its download
  **completes**, not when it is listed.
* The buffer drains at one second per wall-clock second while playing.
* At zero the state becomes `REBUFFERING`; playback resumes once
  `vpb_rebuffer_resume_td_multiple × TARGETDURATION` has accumulated.
* A segment that never downloads adds nothing.
* Above `vpb_max_buffer_s` the model emits `BUFFER_TOO_LONG`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.analysis.rules import catalogue as R
from app.analysis.rules.base import Finding, StreamLayer
from app.config import Thresholds, VpbMode


class BufferState(str, Enum):
    STARTUP = "STARTUP"
    PLAYING = "PLAYING"
    REBUFFERING = "REBUFFERING"
    ENDED = "ENDED"


@dataclass(slots=True)
class SegmentDelivery:
    """One measured segment fetch, as the model consumes it."""

    msn: int
    completed_at: dt.datetime
    duration_s: float
    available: bool = True
    download_ms: float = 0.0
    uri: str = ""


@dataclass(slots=True)
class BufferPoint:
    at: dt.datetime
    level_s: float
    state: BufferState


@dataclass(slots=True)
class Stall:
    started_at: dt.datetime
    ended_at: dt.datetime | None = None
    trigger_msn: int | None = None

    @property
    def duration_s(self) -> float:
        if self.ended_at is None:
            return 0.0
        return (self.ended_at - self.started_at).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_s": self.duration_s,
            "trigger_msn": self.trigger_msn,
        }


@dataclass(slots=True)
class VpbResult:
    variant: str
    mode: VpbMode
    points: list[BufferPoint] = field(default_factory=list)
    stalls: list[Stall] = field(default_factory=list)
    buffer_too_long_events: list[dt.datetime] = field(default_factory=list)
    outage_windows: list[tuple[dt.datetime, dt.datetime]] = field(default_factory=list)
    playing_s: float = 0.0
    stall_s: float = 0.0
    startup_s: float = 0.0
    started_playback: bool = False
    segments_seen: int = 0
    segments_unavailable: int = 0

    @property
    def rebuffer_ratio(self) -> float:
        """Stall time divided by presentation time (§5.2-J)."""
        total = self.playing_s + self.stall_s
        return (self.stall_s / total) if total > 0 else 0.0

    @property
    def counted_stalls(self) -> list[Stall]:
        """Stalls the selected sensitivity mode counts."""
        if self.mode is VpbMode.STRICT:
            return self.stalls
        if self.mode is VpbMode.OUTAGE_ONLY:
            return [
                stall
                for stall in self.stalls
                if any(start <= stall.started_at <= end for start, end in self.outage_windows)
            ]
        return self.stalls

    def as_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "mode": self.mode.value,
            "rebuffer_ratio": self.rebuffer_ratio,
            "playing_s": self.playing_s,
            "stall_s": self.stall_s,
            "startup_s": self.startup_s,
            "started_playback": self.started_playback,
            "stalls": [s.as_dict() for s in self.counted_stalls],
            "stall_count": len(self.counted_stalls),
            "buffer_too_long_count": len(self.buffer_too_long_events),
            "segments_seen": self.segments_seen,
            "segments_unavailable": self.segments_unavailable,
            "series": [
                {"at": p.at.isoformat(), "level_s": p.level_s, "state": p.state.value}
                for p in self.points
            ],
        }


class VirtualPlayerBuffer:
    """Replays measured deliveries through the buffer model for one rung."""

    def __init__(
        self,
        variant: str,
        *,
        target_duration: float,
        thresholds: Thresholds,
        mode: VpbMode | None = None,
    ) -> None:
        self.variant = variant
        self.target_duration = max(target_duration, 0.1)
        self.thresholds = thresholds
        self.mode = mode or thresholds.vpb_mode
        self.startup_target = self.target_duration * thresholds.vpb_startup_buffer_td_multiple
        self.resume_target = self.target_duration * thresholds.vpb_rebuffer_resume_td_multiple

        self.level_s = 0.0
        self.state = BufferState.STARTUP
        self.result = VpbResult(variant=variant, mode=self.mode)
        self._last_at: dt.datetime | None = None
        self._first_at: dt.datetime | None = None
        self._open_stall: Stall | None = None
        self._consecutive_unavailable_from: dt.datetime | None = None
        self._last_unavailable_at: dt.datetime | None = None

    def feed(self, delivery: SegmentDelivery) -> None:
        """Advance the model to this delivery's completion instant."""
        self.result.segments_seen += 1
        if self._first_at is None:
            self._first_at = delivery.completed_at
        self._advance_to(delivery.completed_at)

        if not delivery.available:
            self.result.segments_unavailable += 1
            if self._consecutive_unavailable_from is None:
                self._consecutive_unavailable_from = delivery.completed_at
            self._last_unavailable_at = delivery.completed_at
            self._record_point(delivery.completed_at)
            return

        if self._consecutive_unavailable_from is not None:
            self.result.outage_windows.append(
                (
                    self._consecutive_unavailable_from,
                    self._last_unavailable_at or delivery.completed_at,
                )
            )
            self._consecutive_unavailable_from = None
            self._last_unavailable_at = None

        self.level_s += max(delivery.duration_s, 0.0)

        if self.level_s > self.thresholds.vpb_max_buffer_s:
            self.result.buffer_too_long_events.append(delivery.completed_at)
            # A real player discards what it cannot hold.
            self.level_s = self.thresholds.vpb_max_buffer_s

        if self.state is BufferState.STARTUP and self.level_s >= self.startup_target:
            self.state = BufferState.PLAYING
            self.result.started_playback = True
            self.result.startup_s = (
                (delivery.completed_at - self._first_at).total_seconds() if self._first_at else 0.0
            )
        elif self.state is BufferState.REBUFFERING and self.level_s >= self.resume_target:
            self._close_stall(delivery.completed_at)
            self.state = BufferState.PLAYING

        self._record_point(delivery.completed_at)

    def finish(self, at: dt.datetime) -> VpbResult:
        """Drain to ``at`` and close any open stall or outage."""
        self._advance_to(at)
        if self._open_stall is not None:
            self._close_stall(at)
        if self._consecutive_unavailable_from is not None:
            self.result.outage_windows.append(
                (self._consecutive_unavailable_from, self._last_unavailable_at or at)
            )
            self._consecutive_unavailable_from = None
        self._record_point(at)
        self.state = BufferState.ENDED
        return self.result

    def _advance_to(self, moment: dt.datetime) -> None:
        if self._last_at is None:
            self._last_at = moment
            return
        elapsed = (moment - self._last_at).total_seconds()
        self._last_at = moment
        if elapsed <= 0:
            return

        if self.state is BufferState.STARTUP:
            return

        if self.state is BufferState.REBUFFERING:
            self.result.stall_s += elapsed
            return

        playable = min(self.level_s, elapsed)
        self.level_s -= playable
        self.result.playing_s += playable

        shortfall = elapsed - playable
        if shortfall > 0 or self.level_s <= 0:
            if self._open_stall is None:
                stall_start = moment - dt.timedelta(seconds=shortfall)
                self._open_stall = Stall(started_at=stall_start)
                self.state = BufferState.REBUFFERING
                self.result.stall_s += shortfall
            else:
                self.result.stall_s += shortfall

    def _close_stall(self, at: dt.datetime) -> None:
        if self._open_stall is None:
            return
        self._open_stall.ended_at = at
        self.result.stalls.append(self._open_stall)
        self._open_stall = None

    def _record_point(self, at: dt.datetime) -> None:
        self.result.points.append(
            BufferPoint(at=at, level_s=round(self.level_s, 3), state=self.state)
        )


def run(
    variant: str,
    deliveries: list[SegmentDelivery],
    *,
    target_duration: float,
    thresholds: Thresholds,
    mode: VpbMode | None = None,
    end_at: dt.datetime | None = None,
) -> VpbResult:
    """Convenience wrapper: replay a whole list of deliveries."""
    buffer = VirtualPlayerBuffer(
        variant, target_duration=target_duration, thresholds=thresholds, mode=mode
    )
    for delivery in deliveries:
        buffer.feed(delivery)
    finish_at = end_at or (deliveries[-1].completed_at if deliveries else dt.datetime.now(dt.UTC))
    return buffer.finish(finish_at)


def findings_for(result: VpbResult, *, layer: StreamLayer, thresholds: Thresholds) -> list[Finding]:
    """Turn a replay into findings."""
    findings: list[Finding] = []
    counted = result.counted_stalls
    summed = sum(stall.duration_s for stall in counted)

    for stall in counted:
        if result.mode is VpbMode.NORMAL and stall.duration_s < thresholds.vpb_outage_threshold_s:
            continue
        findings.append(
            R.VPB_STALL.raise_finding(
                f"The modelled player buffer on {result.variant} reached zero at "
                f"{stall.started_at.isoformat()} and stayed empty for {stall.duration_s:.1f} s.",
                evidence={
                    "variant": result.variant,
                    "started_at": stall.started_at.isoformat(),
                    "ended_at": stall.ended_at.isoformat() if stall.ended_at else None,
                    "duration_s": stall.duration_s,
                    "mode": result.mode.value,
                },
                stream_layer=layer,
                variant=result.variant,
                at=stall.started_at,
            )
        )

    for start, end in result.outage_windows:
        duration = (end - start).total_seconds()
        if duration < thresholds.vpb_outage_threshold_s:
            continue
        findings.append(
            R.VPB_OUTAGE.raise_finding(
                f"No segment on {result.variant} was downloadable between {start.isoformat()} "
                f"and {end.isoformat()}, a continuous {duration:.1f} s.",
                evidence={
                    "variant": result.variant,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "duration_s": duration,
                },
                stream_layer=layer,
                variant=result.variant,
                at=start,
            )
        )

    if result.buffer_too_long_events:
        findings.append(
            R.VPB_BUFFER_LONG.raise_finding(
                f"The modelled player buffer on {result.variant} exceeded "
                f"{thresholds.vpb_max_buffer_s:.0f} s on "
                f"{len(result.buffer_too_long_events)} occasion(s), first at "
                f"{result.buffer_too_long_events[0].isoformat()}.",
                evidence={
                    "variant": result.variant,
                    "count": len(result.buffer_too_long_events),
                    "max_buffer_s": thresholds.vpb_max_buffer_s,
                },
                stream_layer=layer,
                variant=result.variant,
                at=result.buffer_too_long_events[0],
            )
        )

    ratio = result.rebuffer_ratio
    if ratio > thresholds.rebuffer_ratio_threshold:
        findings.append(
            R.VPB_RATIO.raise_finding(
                f"The modelled player on {result.variant} spent {result.stall_s:.1f} s stalled "
                f"and {result.playing_s:.1f} s playing, a rebuffering ratio of {ratio:.3f} "
                f"against a threshold of {thresholds.rebuffer_ratio_threshold}.",
                evidence={
                    "variant": result.variant,
                    "rebuffer_ratio": ratio,
                    "stall_s": result.stall_s,
                    "playing_s": result.playing_s,
                    "threshold": thresholds.rebuffer_ratio_threshold,
                    "mode": result.mode.value,
                    "summed_stall_s": summed,
                },
                stream_layer=layer,
                variant=result.variant,
            )
        )
    elif result.started_playback and not counted:
        findings.append(
            R.VPB_OK.raise_finding(
                f"The modelled player on {result.variant} played {result.playing_s:.1f} s across "
                f"{result.segments_seen} segment(s) without the buffer reaching zero.",
                evidence={
                    "variant": result.variant,
                    "playing_s": result.playing_s,
                    "segments": result.segments_seen,
                },
                stream_layer=layer,
                variant=result.variant,
            )
        )
    return findings
