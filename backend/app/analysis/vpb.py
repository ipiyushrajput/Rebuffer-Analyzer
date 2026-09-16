"""Virtual Player Buffer.

Models a Tizen-like player against the delivery timings actually measured, so Aging and
Bulk produce a rebuffering ratio with no player attached, and Realtime can show the model
next to the real hls.js buffer.

The model follows Plus Player's documented buffering configuration:

* The queue is bounded by a **byte cap and a time cap together**, and the smaller binds.
  FHD holds 3 MB or 15 s; UHD holds 60 MB or 15 s. At 7.5 Mbit/s the FHD byte cap is
  3.2 s of media, so a high rung buffers far less than its 15 s suggests — which is why a
  time-only model under-reported rebuffering on exactly the rungs most at risk.
* The profile comes from the tallest rung the ladder offers: 2160p and above is UHD,
  anything below is FHD.
* Playback starts at the startup watermark, 33% of the total, and a resume after an
  underrun waits for 66%. Both are reached on whichever dimension fills first.
* A segment adds its EXTINF and its measured bytes at the wall-clock instant its download
  **completes**, not when it is listed. A segment that never downloads adds nothing.
* The buffer drains at one second per wall-clock second while playing, and the bytes of
  the media played leave with it.
* Playback underruns at the low watermark, 1% of the total, rather than at exactly zero.
* A queue that reaches its cap emits `BUFFER_TOO_LONG`: the real player would have stopped
  fetching, so media arriving past the cap is media the player never asked for.

Every number above is a threshold in `app/config.py`, so a player change is a settings
change rather than a code change.
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
    # Measured transfer size. Zero means the size is unknown, and the byte cap cannot bind
    # on media that never reported one.
    bytes: int = 0


BYTES_PER_MB = 1_000_000


@dataclass(frozen=True, slots=True)
class BufferProfile:
    """The queue Plus Player gives one content type: a byte cap and a time cap together.

    Both caps apply at once and the smaller one binds, which is what makes a high-bitrate
    rung buffer so much less than the time cap alone implies.
    """

    name: str
    total_bytes: float
    total_s: float
    startup_fraction: float
    resume_fraction: float
    low_fraction: float
    # False leaves the queue bounded by time alone. See `vpb_apply_byte_caps`.
    apply_bytes: bool = True

    def at(self, fraction: float) -> tuple[float, float]:
        """The byte and time watermark at this fraction of the total."""
        return self.total_bytes * fraction, self.total_s * fraction

    @property
    def startup(self) -> tuple[float, float]:
        return self.at(self.startup_fraction)

    @property
    def resume(self) -> tuple[float, float]:
        return self.at(self.resume_fraction)

    @property
    def low_s(self) -> float:
        """Playback underruns here. Time only: playback consumes media time, not bytes."""
        return self.total_s * self.low_fraction

    def seconds_at(self, bitrate_bps: float) -> float:
        """What the total holds, in seconds, for media at this rate."""
        if bitrate_bps <= 0 or not self.apply_bytes:
            return self.total_s
        return min(self.total_s, self.total_bytes * 8 / bitrate_bps)

    def as_dict(self) -> dict[str, Any]:
        startup_bytes, startup_s = self.startup
        resume_bytes, resume_s = self.resume
        return {
            "name": self.name,
            "byte_caps_applied": self.apply_bytes,
            "total_mb": round(self.total_bytes / BYTES_PER_MB, 3),
            "total_s": self.total_s,
            "startup_mb": round(startup_bytes / BYTES_PER_MB, 3),
            "startup_s": round(startup_s, 3),
            "resume_mb": round(resume_bytes / BYTES_PER_MB, 3),
            "resume_s": round(resume_s, 3),
            "low_s": round(self.low_s, 3),
        }


def profile_for(top_height: int | None, thresholds: Thresholds) -> BufferProfile:
    """The buffering profile a ladder gets, from the tallest rung it offers.

    A ladder reaching 2160p is a UHD channel and the device gives it the larger queue; one
    topping out at 1080p or below is FHD. The choice is per channel, not per rung, because
    the player configures its queue once for the content it is about to play.
    """
    uhd = top_height is not None and top_height >= thresholds.vpb_uhd_min_height
    total_mb = thresholds.vpb_uhd_total_mb if uhd else thresholds.vpb_fhd_total_mb
    total_s = thresholds.vpb_uhd_total_s if uhd else thresholds.vpb_fhd_total_s
    return BufferProfile(
        name="UHD" if uhd else "FHD",
        total_bytes=total_mb * BYTES_PER_MB,
        total_s=total_s,
        startup_fraction=thresholds.vpb_startup_fraction,
        resume_fraction=thresholds.vpb_resume_fraction,
        low_fraction=thresholds.vpb_low_watermark_fraction,
        apply_bytes=thresholds.vpb_apply_byte_caps,
    )


@dataclass(slots=True)
class _Chunk:
    """One segment sitting in the queue, drained from the head as playback consumes it."""

    seconds: float
    bytes: float


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
    # The queue the replay used, so a report states the player it modelled.
    profile: dict[str, Any] = field(default_factory=dict)
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
            "profile": self.profile,
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
        top_height: int | None = None,
        profile: BufferProfile | None = None,
    ) -> None:
        self.variant = variant
        self.target_duration = max(target_duration, 0.1)
        self.thresholds = thresholds
        self.mode = mode or thresholds.vpb_mode
        self.profile = profile or profile_for(top_height, thresholds)

        self.queue: list[_Chunk] = []
        self.level_s = 0.0
        self.level_bytes = 0.0
        self.state = BufferState.STARTUP
        self.result = VpbResult(variant=variant, mode=self.mode, profile=self.profile.as_dict())
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

        self._enqueue(max(delivery.duration_s, 0.0), max(delivery.bytes, 0), delivery.completed_at)

        if self.state is BufferState.STARTUP and self._reached(*self.profile.startup):
            self.state = BufferState.PLAYING
            self.result.started_playback = True
            self.result.startup_s = (
                (delivery.completed_at - self._first_at).total_seconds() if self._first_at else 0.0
            )
        elif self.state is BufferState.REBUFFERING and self._reached(*self.profile.resume):
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

        playable = self._play(elapsed)
        self.result.playing_s += playable

        shortfall = elapsed - playable
        # The queue underruns at the low watermark, which is where the player stops, not at
        # a level of exactly zero.
        if shortfall > 0 or self.level_s <= self.profile.low_s:
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

    def _reached(self, bytes_mark: float, seconds_mark: float) -> bool:
        """A multiqueue watermark is met on whichever dimension fills first."""
        if self.profile.apply_bytes and self.level_bytes >= bytes_mark:
            return True
        return self.level_s >= seconds_mark

    def _enqueue(self, seconds: float, size: int, at: dt.datetime) -> None:
        """Add one downloaded segment, up to whichever cap it reaches.

        A real player that has filled its queue stops requesting, so media beyond the cap is
        media it never asked for. The model records that rather than pretending the queue
        grew: the level is what the device could hold.
        """
        if seconds <= 0 and size <= 0:
            return
        capped_on_bytes = self.profile.apply_bytes and self.level_bytes >= self.profile.total_bytes
        if self.level_s >= self.profile.total_s or capped_on_bytes:
            self.result.buffer_too_long_events.append(at)
            return

        room_s = self.profile.total_s - self.level_s
        room_bytes = self.profile.total_bytes - self.level_bytes
        # Bytes and seconds are trimmed together so the chunk keeps the rate it arrived at.
        keep = 1.0
        if seconds > room_s:
            keep = min(keep, room_s / seconds)
        if self.profile.apply_bytes and size > 0 and size > room_bytes:
            keep = min(keep, room_bytes / size)
        if keep < 1.0:
            self.result.buffer_too_long_events.append(at)

        chunk = _Chunk(seconds=seconds * keep, bytes=size * keep)
        self.queue.append(chunk)
        self.level_s += chunk.seconds
        self.level_bytes += chunk.bytes

    def _play(self, elapsed: float) -> float:
        """Consume up to `elapsed` seconds from the head, taking their bytes with them."""
        played = 0.0
        remaining = elapsed
        while remaining > 0 and self.queue:
            head = self.queue[0]
            if head.seconds <= remaining:
                played += head.seconds
                remaining -= head.seconds
                self.level_s -= head.seconds
                self.level_bytes -= head.bytes
                self.queue.pop(0)
                continue
            share = remaining / head.seconds if head.seconds > 0 else 1.0
            gone_bytes = head.bytes * share
            head.seconds -= remaining
            head.bytes -= gone_bytes
            self.level_s -= remaining
            self.level_bytes -= gone_bytes
            played += remaining
            remaining = 0.0
        # Floating-point residue must not leave a queue that reads as non-empty.
        if not self.queue:
            self.level_s = 0.0
            self.level_bytes = 0.0
        return played

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
    top_height: int | None = None,
) -> VpbResult:
    """Convenience wrapper: replay a whole list of deliveries."""
    buffer = VirtualPlayerBuffer(
        variant,
        target_duration=target_duration,
        thresholds=thresholds,
        mode=mode,
        top_height=top_height,
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
        profile = result.profile
        # Name the caps that are in force, so the finding states the queue it measured.
        cap = f"{profile['total_s']:.0f} s"
        if profile.get("byte_caps_applied"):
            cap = f"{profile['total_mb']:.0f} MB or {cap}"
        findings.append(
            R.VPB_BUFFER_LONG.raise_finding(
                f"The modelled {profile['name']} player queue on {result.variant} reached its "
                f"cap of {cap} on "
                f"{len(result.buffer_too_long_events)} occasion(s), first at "
                f"{result.buffer_too_long_events[0].isoformat()}.",
                evidence={
                    "variant": result.variant,
                    "count": len(result.buffer_too_long_events),
                    "profile": profile,
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
