"""Media playlist detectors.

Two kinds of check live here: a snapshot check that judges a single playlist, and a
transition check that compares two consecutive polls. The playlist state machine
(`UNKNOWN → LIVE | STALLED | HTTP_ERROR | LIVE_END | VOD`) drives the freshness rules.
"""

from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.analysis.rules import catalogue as R
from app.analysis.rules.base import Finding, StreamLayer
from app.config import Thresholds
from app.hls.playlist import MediaPlaylist

ROUNDING_TOLERANCE_S = 0.5


class PlaylistState(str, Enum):
    UNKNOWN = "UNKNOWN"
    LIVE = "LIVE"
    STALLED = "STALLED"
    HTTP_ERROR = "HTTP_ERROR"
    LIVE_END = "LIVE_END"
    VOD = "VOD"


@dataclass(slots=True)
class StateTransition:
    at: dt.datetime
    previous: PlaylistState
    current: PlaylistState
    reason: str


@dataclass(slots=True)
class PlaylistStateMachine:
    """Tracks one media playlist's state over the life of a job."""

    variant: str
    state: PlaylistState = PlaylistState.UNKNOWN
    transitions: list[StateTransition] = field(default_factory=list)
    was_live: bool = False
    last_new_segment_at: dt.datetime | None = None
    last_msn: int | None = None

    def observe(
        self,
        *,
        at: dt.datetime,
        http_ok: bool,
        playlist: MediaPlaylist | None,
        stale_after_s: float,
    ) -> StateTransition | None:
        previous = self.state
        reason = ""

        if not http_ok or playlist is None:
            self.state = PlaylistState.HTTP_ERROR
            reason = "The playlist request did not return a usable body."
        elif playlist.endlist and self.was_live:
            self.state = PlaylistState.LIVE_END
            reason = "EXT-X-ENDLIST appeared after the playlist had been live."
        elif playlist.endlist and not self.was_live:
            self.state = PlaylistState.VOD
            reason = "The playlist carries EXT-X-ENDLIST and was never live."
        else:
            advanced = self.last_msn is None or playlist.last_msn > self.last_msn
            if advanced:
                self.last_new_segment_at = at
                self.state = PlaylistState.LIVE
                self.was_live = True
                reason = "A new segment appeared."
            else:
                since = (
                    (at - self.last_new_segment_at).total_seconds()
                    if self.last_new_segment_at
                    else 0.0
                )
                if since >= stale_after_s:
                    self.state = PlaylistState.STALLED
                    reason = f"No new segment appeared for {since:.1f} s."
                else:
                    self.state = PlaylistState.LIVE if self.was_live else self.state
                    reason = "The playlist is unchanged inside the stale window."
            self.last_msn = playlist.last_msn if playlist else self.last_msn

        if self.state == previous:
            return None
        transition = StateTransition(at=at, previous=previous, current=self.state, reason=reason)
        self.transitions.append(transition)
        return transition

    def seconds_since_new_segment(self, at: dt.datetime) -> float:
        if self.last_new_segment_at is None:
            return 0.0
        return (at - self.last_new_segment_at).total_seconds()


def check_media_playlist(
    playlist: MediaPlaylist,
    *,
    variant: str,
    layer: StreamLayer,
    thresholds: Thresholds,
    is_subtitle: bool = False,
) -> list[Finding]:
    """Judge one playlist snapshot."""
    findings: list[Finding] = []
    url = playlist.final_url
    evidence_base: dict[str, Any] = {"url": url, "variant": variant}

    if playlist.looks_like_master:
        findings.append(
            R.MED_BECAME_MASTER.raise_finding(
                f"{url} returns EXT-X-STREAM-INF entries where segments were listed.",
                evidence=evidence_base,
                stream_layer=layer,
                variant=variant,
            )
        )
        return findings

    target = playlist.target_duration
    if target is None:
        findings.append(
            R.MED_EXTINF_INVALID.raise_finding(
                f"{url} declares no EXT-X-TARGETDURATION.",
                evidence=evidence_base,
                stream_layer=layer,
                variant=variant,
            )
        )

    for segment in playlist.segments:
        seg_evidence = {
            **evidence_base,
            "msn": segment.msn,
            "uri": segment.resolved_uri,
            "line": segment.line.number,
            "raw_line": segment.line.text.strip(),
            "extinf": segment.duration,
        }
        if segment.duration <= 0:
            findings.append(
                R.MED_EXTINF_INVALID.raise_finding(
                    f"Segment {segment.msn} on {variant} declares EXTINF "
                    f"{segment.duration} on line {segment.line.number}.",
                    evidence=seg_evidence,
                    stream_layer=layer,
                    variant=variant,
                )
            )
            continue
        if target and round(segment.duration) > target + ROUNDING_TOLERANCE_S:
            findings.append(
                R.MED_TARGETDURATION.raise_finding(
                    f"Segment {segment.msn} on {variant} declares EXTINF "
                    f"{segment.duration:.3f} s, which rounds to {round(segment.duration)} s "
                    f"against EXT-X-TARGETDURATION:{target:.0f}.",
                    evidence={**seg_evidence, "target_duration": target},
                    stream_layer=layer,
                    variant=variant,
                )
            )
        if target and segment.duration > target * thresholds.segment_extinf_max_ratio_to_td:
            findings.append(
                R.MED_EXTINF_OVER_TD.raise_finding(
                    f"Segment {segment.msn} on {variant} declares EXTINF "
                    f"{segment.duration:.3f} s, which is "
                    f"{segment.duration / target:.2f}x the target duration of {target:.0f} s.",
                    evidence={**seg_evidence, "target_duration": target},
                    stream_layer=layer,
                    variant=variant,
                )
            )
        if segment.duration > thresholds.segment_extinf_absolute_max_s:
            findings.append(
                R.MED_EXTINF_ABSOLUTE.raise_finding(
                    f"Segment {segment.msn} on {variant} declares EXTINF "
                    f"{segment.duration:.1f} s against an absolute maximum of "
                    f"{thresholds.segment_extinf_absolute_max_s:.0f} s.",
                    evidence=seg_evidence,
                    stream_layer=layer,
                    variant=variant,
                )
            )
        if segment.gap:
            findings.append(
                R.MED_GAP.raise_finding(
                    f"Segment {segment.msn} on {variant} is marked EXT-X-GAP on line "
                    f"{segment.line.number}, covering {segment.duration:.3f} s.",
                    evidence=seg_evidence,
                    stream_layer=layer,
                    variant=variant,
                )
            )
        if segment.byterange and not _byterange_valid(segment.byterange):
            findings.append(
                R.MED_BYTERANGE_INVALID.raise_finding(
                    f"Segment {segment.msn} on {variant} declares "
                    f"EXT-X-BYTERANGE:{segment.byterange}, which does not parse.",
                    evidence=seg_evidence,
                    stream_layer=layer,
                    variant=variant,
                )
            )

    durations = [s.duration for s in playlist.segments if s.duration > 0]
    if len(durations) >= 3:
        spread = max(durations) - min(durations)
        if spread > thresholds.extinf_vs_actual_tolerance_s * 10:
            findings.append(
                R.MED_EXTINF_VARIANCE.raise_finding(
                    f"{variant} lists segment durations from {min(durations):.3f} s to "
                    f"{max(durations):.3f} s, a spread of {spread:.3f} s.",
                    evidence={**evidence_base, "min": min(durations), "max": max(durations)},
                    stream_layer=layer,
                    variant=variant,
                )
            )

    if playlist.is_live and target:
        minimum = target * thresholds.min_live_window_multiple
        if playlist.total_duration < minimum:
            findings.append(
                R.MED_WINDOW_SHORT.raise_finding(
                    f"{variant} publishes a live window of {playlist.total_duration:.1f} s across "
                    f"{len(playlist.segments)} segment(s) against a minimum of {minimum:.1f} s "
                    f"({thresholds.min_live_window_multiple} x target duration {target:.0f} s).",
                    evidence={
                        **evidence_base,
                        "window_s": playlist.total_duration,
                        "minimum_s": minimum,
                    },
                    stream_layer=layer,
                    variant=variant,
                )
            )

    if playlist.map_uri is None and _is_fmp4(playlist):
        findings.append(
            R.MED_MAP_MISSING.raise_finding(
                f"{variant} lists fMP4 segments and declares no EXT-X-MAP.",
                evidence=evidence_base,
                stream_layer=layer,
                variant=variant,
            )
        )

    findings += _check_pdt(playlist, variant=variant, layer=layer, thresholds=thresholds)

    if not is_subtitle and not any(f.severity.rank >= 2 for f in findings):
        summary = (
            f"{variant} lists {len(playlist.segments)} segment(s) spanning "
            f"{playlist.total_duration:.1f} s"
        )
        if target:
            summary += f" with EXT-X-TARGETDURATION:{target:.0f}"
        findings.append(
            R.MED_OK.raise_finding(
                summary + ".",
                evidence=evidence_base,
                stream_layer=layer,
                variant=variant,
            )
        )
    return findings


def _byterange_valid(value: str) -> bool:
    parts = value.split("@")
    if not parts[0].strip().isdigit():
        return False
    if len(parts) == 2 and not parts[1].strip().isdigit():
        return False
    return len(parts) <= 2


def _is_fmp4(playlist: MediaPlaylist) -> bool:
    return any(
        s.uri.split("?")[0].lower().endswith((".m4s", ".mp4", ".cmfv", ".cmfa"))
        for s in playlist.segments
    )


def _check_pdt(
    playlist: MediaPlaylist, *, variant: str, layer: StreamLayer, thresholds: Thresholds
) -> list[Finding]:
    findings: list[Finding] = []
    stamped = [s for s in playlist.segments if s.program_date_time]
    if not stamped:
        findings.append(
            R.MED_PDT_MISSING.raise_finding(
                f"{variant} carries no EXT-X-PROGRAM-DATE-TIME across "
                f"{len(playlist.segments)} segment(s).",
                evidence={"url": playlist.final_url, "variant": variant},
                stream_layer=layer,
                variant=variant,
            )
        )
        return findings

    parsed: list[tuple[int, dt.datetime]] = []
    for segment in stamped:
        moment = parse_pdt(segment.program_date_time or "")
        if moment is not None:
            parsed.append((segment.msn, moment))

    for (msn_a, time_a), (msn_b, time_b) in itertools.pairwise(parsed):
        if time_b < time_a:
            findings.append(
                R.MED_PDT_NON_MONOTONIC.raise_finding(
                    f"{variant} stamps segment {msn_b} at {time_b.isoformat()} after segment "
                    f"{msn_a} at {time_a.isoformat()}.",
                    evidence={"variant": variant, "msn_a": msn_a, "msn_b": msn_b},
                    stream_layer=layer,
                    variant=variant,
                )
            )

    if len(parsed) >= 2:
        first_msn, first_time = parsed[0]
        last_msn, last_time = parsed[-1]
        wall = (last_time - first_time).total_seconds()
        media = sum(
            s.duration
            for s in playlist.segments
            if first_msn <= s.msn < last_msn and s.duration > 0
        )
        drift = abs(wall - media)
        if media > 0 and drift > max(1.0, media * 0.02):
            findings.append(
                R.MED_PDT_DRIFT.raise_finding(
                    f"{variant} spans {media:.2f} s of EXTINF between segments {first_msn} and "
                    f"{last_msn} while its program date times span {wall:.2f} s, a drift of "
                    f"{drift:.2f} s.",
                    evidence={
                        "variant": variant,
                        "wall_s": wall,
                        "media_s": media,
                        "drift_s": drift,
                    },
                    stream_layer=layer,
                    variant=variant,
                )
            )
    return findings


def parse_pdt(value: str) -> dt.datetime | None:
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def check_freshness(
    *,
    machine: PlaylistStateMachine,
    at: dt.datetime,
    target_duration: float | None,
    variant: str,
    layer: StreamLayer,
    thresholds: Thresholds,
) -> Finding | None:
    """The primary rebuffer cause: no new segment inside the stale window."""
    if target_duration is None or machine.last_new_segment_at is None:
        return None
    stale_after = target_duration * thresholds.stale_playlist_factor
    since = machine.seconds_since_new_segment(at)
    if since < stale_after:
        return None
    return R.MED_STALE.raise_finding(
        f"{variant} published no new segment for {since:.1f} s against a stale window of "
        f"{stale_after:.1f} s ({thresholds.stale_playlist_factor} x target duration "
        f"{target_duration:.0f} s). The last new segment arrived at "
        f"{machine.last_new_segment_at.isoformat()}.",
        evidence={
            "variant": variant,
            "stale_s": since,
            "stale_after_s": stale_after,
            "last_new_segment_at": machine.last_new_segment_at.isoformat(),
            "last_msn": machine.last_msn,
        },
        stream_layer=layer,
        variant=variant,
        at=at,
    )


def check_state_transition(
    transition: StateTransition, *, variant: str, layer: StreamLayer
) -> Finding | None:
    if transition.current == PlaylistState.LIVE:
        return None
    if transition.previous == PlaylistState.UNKNOWN and transition.current in (
        PlaylistState.VOD,
        PlaylistState.LIVE,
    ):
        return None

    rule = R.MED_STATE_TRANSITION
    if transition.current == PlaylistState.LIVE_END:
        rule = R.MED_UNEXPECTED_ENDLIST
    elif transition.current == PlaylistState.HTTP_ERROR:
        rule = R.MED_DOWNLOAD_FAIL

    return rule.raise_finding(
        f"{variant} moved from {transition.previous.value} to {transition.current.value} at "
        f"{transition.at.isoformat()}. {transition.reason}",
        evidence={
            "variant": variant,
            "from": transition.previous.value,
            "to": transition.current.value,
            "at": transition.at.isoformat(),
            "reason": transition.reason,
        },
        stream_layer=layer,
        variant=variant,
        at=transition.at,
    )


def check_publication_rate(
    *,
    added_duration_s: float,
    elapsed_wall_s: float,
    variant: str,
    layer: StreamLayer,
    factor: float = 1.5,
) -> Finding | None:
    """Segments published faster than real time push the player away from the live edge."""
    if elapsed_wall_s <= 0 or added_duration_s <= 0:
        return None
    if added_duration_s <= elapsed_wall_s * factor:
        return None
    return R.MED_BUFFER_TOO_LONG.raise_finding(
        f"{variant} added {added_duration_s:.1f} s of media in {elapsed_wall_s:.1f} s of wall "
        f"time, a factor of {added_duration_s / elapsed_wall_s:.2f}.",
        evidence={
            "variant": variant,
            "added_duration_s": added_duration_s,
            "elapsed_wall_s": elapsed_wall_s,
        },
        stream_layer=layer,
        variant=variant,
    )


def check_subtitle_stuck(
    previous: MediaPlaylist, current: MediaPlaylist, *, variant: str, layer: StreamLayer
) -> Finding | None:
    """A subtitle playlist whose MSN advances while its segment list stays identical."""
    if current.media_sequence <= previous.media_sequence:
        return None
    if [s.uri for s in previous.segments] != [s.uri for s in current.segments]:
        return None
    return R.MED_SUBTITLE_STUCK.raise_finding(
        f"{variant} advanced EXT-X-MEDIA-SEQUENCE from {previous.media_sequence} to "
        f"{current.media_sequence} while listing the same {len(current.segments)} segment(s): "
        f"{[s.uri for s in current.segments][:3]}.",
        evidence={
            "variant": variant,
            "previous_msn": previous.media_sequence,
            "current_msn": current.media_sequence,
            "segments": [s.uri for s in current.segments][:5],
        },
        stream_layer=layer,
        variant=variant,
    )


def check_segment_uri_changed(
    previous: MediaPlaylist, current: MediaPlaylist, *, variant: str, layer: StreamLayer
) -> list[Finding]:
    """The same media sequence number served under a different URI between polls."""
    findings: list[Finding] = []
    before = {s.msn: s.uri for s in previous.segments}
    for segment in current.segments:
        old = before.get(segment.msn)
        if old is not None and old != segment.uri:
            findings.append(
                R.MED_URI_CHANGED.raise_finding(
                    f"Media sequence {segment.msn} on {variant} was listed as {old} and is now "
                    f"listed as {segment.uri}.",
                    evidence={
                        "variant": variant,
                        "msn": segment.msn,
                        "previous_uri": old,
                        "current_uri": segment.uri,
                    },
                    stream_layer=layer,
                    variant=variant,
                )
            )
    return findings
