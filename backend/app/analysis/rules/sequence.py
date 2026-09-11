"""Sequence detectors: media sequence, discontinuity sequence, and cross-variant agreement.

The transition rules compare two consecutive polls of the same playlist. The cross-variant
rules compare one poll of every rendition at the same moment — the check that catches the
Tizen freeze, because the player holds one discontinuity counter for the whole ladder.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from app.analysis.rules import catalogue as R
from app.analysis.rules.base import Finding, StreamLayer
from app.config import Thresholds
from app.hls.playlist import MediaPlaylist

# Shared with the frontend's SequenceLadder: below this a spread is ordinary poll skew.
MSN_GAP_TOLERANCE = 5


@dataclass(slots=True)
class VariantSnapshot:
    """One rendition's playlist as it was at one moment."""

    variant: str
    playlist: MediaPlaylist
    at: dt.datetime
    kind: str = "video"  # video | audio | subtitles


def check_sequence_transition(
    previous: MediaPlaylist,
    current: MediaPlaylist,
    *,
    variant: str,
    layer: StreamLayer,
    at: dt.datetime | None = None,
    cdn_headers: dict[str, str] | None = None,
) -> list[Finding]:
    """Compare two consecutive polls of the same media playlist."""
    findings: list[Finding] = []
    moment = at or dt.datetime.now(dt.UTC)
    base: dict[str, Any] = {
        "variant": variant,
        "previous_msn": previous.media_sequence,
        "current_msn": current.media_sequence,
        "previous_last_msn": previous.last_msn,
        "current_last_msn": current.last_msn,
        "previous_count": len(previous.segments),
        "current_count": len(current.segments),
    }

    delta = current.media_sequence - previous.media_sequence

    if delta < 0:
        findings.append(
            R.SEQ_MSN_BACKWARDS.raise_finding(
                f"{variant} returned EXT-X-MEDIA-SEQUENCE:{current.media_sequence} after "
                f"{previous.media_sequence}, moving back {abs(delta)} segment(s)."
                + (f" Response headers: {cdn_headers}." if cdn_headers else ""),
                evidence={**base, "cdn_headers": cdn_headers or {}},
                stream_layer=layer,
                variant=variant,
                at=moment,
            )
        )
        if cdn_headers and (cdn_headers.get("x-cache") or cdn_headers.get("age")):
            findings.append(
                R.CDN_WRONG_MSN.raise_finding(
                    f"{variant} returned media sequence {current.media_sequence} after "
                    f"{previous.media_sequence} with {cdn_headers}.",
                    evidence={**base, "cdn_headers": cdn_headers},
                    stream_layer=layer,
                    variant=variant,
                    at=moment,
                )
            )
        if current.media_sequence < previous.media_sequence / 2 and not current.endlist:
            findings.append(
                R.SEQ_MSN_WRAP.raise_finding(
                    f"{variant} reset EXT-X-MEDIA-SEQUENCE from {previous.media_sequence} to "
                    f"{current.media_sequence} without EXT-X-ENDLIST.",
                    evidence=base,
                    stream_layer=layer,
                    variant=variant,
                    at=moment,
                )
            )
        return findings

    if delta == 0:
        previous_uris = [s.uri for s in previous.segments]
        current_uris = [s.uri for s in current.segments]
        if previous_uris == current_uris:
            return findings
        return findings

    rolled = _segments_rolled_off(previous, current)
    if rolled is not None and delta != rolled:
        findings.append(
            R.SEQ_MSN_INCREMENT.raise_finding(
                f"{variant} advanced EXT-X-MEDIA-SEQUENCE by {delta} while {rolled} segment(s) "
                f"rolled off the window.",
                evidence={**base, "rolled": rolled, "delta": delta},
                stream_layer=layer,
                variant=variant,
                at=moment,
            )
        )

    if delta > max(1, len(previous.segments)):
        findings.append(
            R.SEQ_MSN_JUMP.raise_finding(
                f"{variant} advanced EXT-X-MEDIA-SEQUENCE by {delta} between polls while the "
                f"window holds {len(previous.segments)} segment(s).",
                evidence=base,
                stream_layer=layer,
                variant=variant,
                at=moment,
            )
        )

    if delta > 0 and [s.uri for s in previous.segments] == [s.uri for s in current.segments]:
        findings.append(
            R.SEQ_MSN_LIST_IDENTICAL.raise_finding(
                f"{variant} advanced EXT-X-MEDIA-SEQUENCE from {previous.media_sequence} to "
                f"{current.media_sequence} while listing the same "
                f"{len(current.segments)} segment(s).",
                evidence={**base, "segments": [s.uri for s in current.segments][:5]},
                stream_layer=layer,
                variant=variant,
                at=moment,
            )
        )

    findings += _check_dsn(previous, current, variant=variant, layer=layer, at=moment, base=base)
    return findings


def _segments_rolled_off(previous: MediaPlaylist, current: MediaPlaylist) -> int | None:
    """How many of the previous window's segments are gone from the current window."""
    previous_uris = [s.uri for s in previous.segments]
    current_uris = {s.uri for s in current.segments}
    if not previous_uris:
        return None
    rolled = 0
    for uri in previous_uris:
        if uri in current_uris:
            break
        rolled += 1
    return rolled


def _check_dsn(
    previous: MediaPlaylist,
    current: MediaPlaylist,
    *,
    variant: str,
    layer: StreamLayer,
    at: dt.datetime,
    base: dict[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    evidence = {
        **base,
        "previous_dsn": previous.discontinuity_sequence,
        "current_dsn": current.discontinuity_sequence,
    }
    delta = current.discontinuity_sequence - previous.discontinuity_sequence
    if delta < 0:
        findings.append(
            R.SEQ_DSN_BACKWARDS.raise_finding(
                f"{variant} returned EXT-X-DISCONTINUITY-SEQUENCE:"
                f"{current.discontinuity_sequence} after {previous.discontinuity_sequence}.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant,
                at=at,
            )
        )
    elif delta > 1:
        findings.append(
            R.SEQ_DSN_JUMP.raise_finding(
                f"{variant} advanced EXT-X-DISCONTINUITY-SEQUENCE by {delta}, from "
                f"{previous.discontinuity_sequence} to {current.discontinuity_sequence}, "
                "between two polls.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant,
                at=at,
            )
        )
    return findings


def check_cross_variant(
    snapshots: list[VariantSnapshot],
    *,
    layer: StreamLayer,
    thresholds: Thresholds,
    at: dt.datetime | None = None,
) -> list[Finding]:
    """Compare every rendition at the same moment."""
    findings: list[Finding] = []
    moment = at or dt.datetime.now(dt.UTC)
    video = [s for s in snapshots if s.kind == "video"]
    if len(snapshots) < 2:
        return findings

    msns = {s.variant: s.playlist.last_msn for s in video} or {
        s.variant: s.playlist.last_msn for s in snapshots
    }
    if len(msns) >= 2:
        spread = max(msns.values()) - min(msns.values())
        if spread >= thresholds.cross_variant_msn_error_spread:
            leader = max(msns, key=lambda k: msns[k])
            laggard = min(msns, key=lambda k: msns[k])
            findings.append(
                R.SEQ_XVAR_MSN_SPREAD.raise_finding(
                    f"Media sequence numbers span {spread} across the ladder at "
                    f"{moment.isoformat()}: {leader} is at {msns[leader]} and {laggard} is at "
                    f"{msns[laggard]}, against a tolerance of "
                    f"{thresholds.cross_variant_msn_error_spread}.",
                    evidence={"msn_by_variant": msns, "spread": spread, "at": moment.isoformat()},
                    stream_layer=layer,
                    at=moment,
                )
            )

    dsns = {s.variant: s.playlist.discontinuity_sequence for s in snapshots}
    if len(set(dsns.values())) > 1:
        findings.append(
            R.SEQ_XVAR_DSN_MISMATCH.raise_finding(
                f"Discontinuity sequence numbers differ across the ladder at "
                f"{moment.isoformat()}: {dsns}. The Tizen player holds one counter for every "
                "rendition.",
                evidence={"dsn_by_variant": dsns, "at": moment.isoformat()},
                stream_layer=layer,
                at=moment,
            )
        )

        audio = [s for s in snapshots if s.kind == "audio"]
        if video and audio:
            video_dsn = {s.variant: s.playlist.discontinuity_sequence for s in video}
            audio_dsn = {s.variant: s.playlist.discontinuity_sequence for s in audio}
            if set(video_dsn.values()) != set(audio_dsn.values()):
                findings.append(
                    R.AV_DSN_MISMATCH.raise_finding(
                        f"Video renditions declare discontinuity sequence "
                        f"{sorted(set(video_dsn.values()))} and audio renditions declare "
                        f"{sorted(set(audio_dsn.values()))}.",
                        evidence={"video": video_dsn, "audio": audio_dsn},
                        stream_layer=layer,
                        at=moment,
                    )
                )

    findings += _check_common_msn_alignment(video, layer=layer, at=moment)
    findings += _check_discontinuity_positions(video, layer=layer, at=moment)
    findings += _check_pdt_alignment(snapshots, layer=layer, at=moment)
    return findings


def _check_common_msn_alignment(
    video: list[VariantSnapshot], *, layer: StreamLayer, at: dt.datetime
) -> list[Finding]:
    """Every rung must cover the same span of the timeline for the same MSN."""
    findings: list[Finding] = []
    if len(video) < 2:
        return findings

    ranges = [
        set(range(s.playlist.media_sequence, s.playlist.last_msn + 1))
        for s in video
        if s.playlist.segments
    ]
    if not ranges:
        return findings
    common = set.intersection(*ranges)
    if not common:
        return findings

    for msn in sorted(common)[:20]:
        durations = {}
        for snapshot in video:
            segment = snapshot.playlist.segment_by_msn(msn)
            if segment:
                durations[snapshot.variant] = round(segment.duration, 3)
        if len(set(durations.values())) > 1:
            findings.append(
                R.SEQ_XVAR_SEGMENT_MISMATCH.raise_finding(
                    f"Media sequence {msn} declares different durations across the ladder: "
                    f"{durations}.",
                    evidence={"msn": msn, "durations": durations},
                    stream_layer=layer,
                    at=at,
                )
            )
            break
    return findings


def _check_discontinuity_positions(
    video: list[VariantSnapshot], *, layer: StreamLayer, at: dt.datetime
) -> list[Finding]:
    findings: list[Finding] = []
    if len(video) < 2:
        return findings
    positions = {
        s.variant: sorted(seg.msn for seg in s.playlist.segments if seg.discontinuity_before)
        for s in video
    }
    unique = {tuple(v) for v in positions.values()}
    if len(unique) > 1:
        findings.append(
            R.SEQ_XVAR_DISC_POSITION.raise_finding(
                f"Discontinuity tags sit at different media sequence numbers across the ladder: "
                f"{positions}.",
                evidence={"positions": positions, "at": at.isoformat()},
                stream_layer=layer,
                at=at,
            )
        )
    return findings


def _check_pdt_alignment(
    snapshots: list[VariantSnapshot], *, layer: StreamLayer, at: dt.datetime
) -> list[Finding]:
    from app.analysis.rules.media_playlist import parse_pdt

    findings: list[Finding] = []
    firsts: dict[str, dt.datetime] = {}
    for snapshot in snapshots:
        for segment in snapshot.playlist.segments:
            if segment.program_date_time:
                moment = parse_pdt(segment.program_date_time)
                if moment:
                    firsts[snapshot.variant] = moment
                break
    if len(firsts) < 2:
        return findings
    spread = (max(firsts.values()) - min(firsts.values())).total_seconds()
    if spread > 1.0:
        findings.append(
            R.SEQ_XVAR_PDT.raise_finding(
                f"The first program date time differs by {spread:.2f} s across renditions: "
                + ", ".join(f"{k} at {v.isoformat()}" for k, v in sorted(firsts.items()))
                + ".",
                evidence={"pdt_by_variant": {k: v.isoformat() for k, v in firsts.items()}},
                stream_layer=layer,
                at=at,
            )
        )
    return findings
