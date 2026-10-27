"""Sequence detectors: media sequence, discontinuity sequence, and cross-variant agreement.

The transition rules compare two consecutive polls of the same playlist. The cross-variant
rules compare one poll of every rendition at the same moment — the check that catches the
Tizen freeze, because the player holds one discontinuity counter for the whole ladder. "The
same moment" is one `LadderPoller` round: every rendition requested together. The engine
records a cross-variant finding only once it holds for `cross_variant_confirm_rounds`
consecutive rounds.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from app.analysis.rules import catalogue as R
from app.analysis.rules.base import Finding, StreamLayer
from app.config import Thresholds
from app.hls.playlist import MediaPlaylist
from app.hls.uri import uri_path

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
    elapsed_s: float | None = None,
) -> list[Finding]:
    """Compare two consecutive polls of the same media playlist.

    `elapsed_s` is the wall time between the two polls. A playlist that advanced by about as
    many segments as that time holds was polled late, not one that jumped.
    """
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

    target = current.target_duration or previous.target_duration or 0.0
    # How far the live edge can honestly move in the time between the two polls, plus one
    # segment for the publication that can land on either side of a poll.
    explained = (elapsed_s / target + 1) if elapsed_s is not None and target > 0 else 0.0
    if delta > max(1, len(previous.segments)) and delta > explained:
        findings.append(
            R.SEQ_MSN_JUMP.raise_finding(
                f"{variant} advanced EXT-X-MEDIA-SEQUENCE by {delta} between polls while the "
                f"window holds {len(previous.segments)} segment(s)"
                + (
                    f" and {elapsed_s:.1f} s passed between the two polls, which accounts for "
                    f"{explained:.0f} segment(s) of target duration {target:g} s."
                    if elapsed_s is not None and target > 0
                    else "."
                ),
                evidence={**base, "elapsed_s": elapsed_s, "explained_segments": explained},
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
    """How many of the previous window's segments are gone from the current window.

    Segments are matched on their path: a query string re-signed on every response would
    otherwise make every segment look gone.
    """
    previous_uris = [uri_path(s.uri) for s in previous.segments]
    current_uris = {uri_path(s.uri) for s in current.segments}
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
    # Every discontinuity that left the window between the two polls moves into the counter,
    # and so may one on the segment that is now first (a packager folds that tag in as soon
    # as the segment leads the window). Only an advance beyond those is a jump.
    rolled_tags = sum(
        1
        for segment in previous.segments
        if segment.discontinuity_before and segment.msn <= current.media_sequence
    )
    overlap = previous.segments and previous.last_msn >= current.media_sequence
    evidence["discontinuities_rolled_off"] = rolled_tags
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
    elif delta > max(1, rolled_tags) and overlap:
        findings.append(
            R.SEQ_DSN_JUMP.raise_finding(
                f"{variant} advanced EXT-X-DISCONTINUITY-SEQUENCE by {delta}, from "
                f"{previous.discontinuity_sequence} to {current.discontinuity_sequence}, "
                f"between two polls while {rolled_tags} discontinuity tag(s) left the window.",
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

    dsn_findings = _check_dsn_mismatch(snapshots, layer=layer, at=moment, thresholds=thresholds)
    findings += dsn_findings
    if dsn_findings:
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

    findings += _check_common_msn_alignment(
        video, layer=layer, at=moment, tolerance_s=thresholds.cross_variant_duration_tolerance_s
    )
    findings += _check_discontinuity_positions(video, layer=layer, at=moment)
    findings += _check_pdt_alignment(
        snapshots, layer=layer, at=moment, tolerance_s=thresholds.cross_variant_pdt_tolerance_s
    )
    return findings


def comparable_msns(snapshots: list[VariantSnapshot]) -> set[int]:
    """The media sequence numbers every rendition lists, first segment excluded.

    Two renditions are only comparable where both actually publish the same segment. The
    first segment of a window is excluded on top of that: a discontinuity there is not
    written as a tag at all, because the packager folds it into
    EXT-X-DISCONTINUITY-SEQUENCE and drops the tag as the window slides. Renditions slide at
    different moments, so the first segment disagrees on paper while the timeline agrees.
    """
    windows: list[set[int]] = []
    for snapshot in snapshots:
        msns = [segment.msn for segment in snapshot.playlist.segments]
        if len(msns) < 2:
            return set()
        windows.append(set(msns[1:]))
    return set.intersection(*windows) if windows else set()


def _dsn_at_msn(snapshot: VariantSnapshot, msn: int) -> int | None:
    segment = snapshot.playlist.segment_by_msn(msn)
    return segment.discontinuity_sequence if segment else None


def _check_dsn_mismatch(
    snapshots: list[VariantSnapshot],
    *,
    layer: StreamLayer,
    at: dt.datetime,
    thresholds: Thresholds,
) -> list[Finding]:
    """Discontinuity counters that disagree by more than independent polling explains.

    Even within one poll round a packager publishes each rendition a moment apart, so one can
    carry a discontinuity before the next publishes it and the declared counters differ for a
    round. That is publication order, not a ladder defect, and the engine's round confirmation
    drops it. Two measurements separate the defect from the skew: a
    spread wider than the tolerance, which no poll ordering produces, and a disagreement at
    one media sequence number every rendition lists, which is the same instant of the
    timeline seen from every rung.
    """
    dsns = {s.variant: s.playlist.discontinuity_sequence for s in snapshots}
    if len(set(dsns.values())) <= 1:
        return []

    spread = max(dsns.values()) - min(dsns.values())
    evidence: dict[str, Any] = {
        "dsn_by_variant": dsns,
        "spread": spread,
        "tolerance": thresholds.cross_variant_dsn_tolerance,
        "polled_at": {s.variant: s.at.isoformat() for s in snapshots},
        "at": at.isoformat(),
    }

    if spread > thresholds.cross_variant_dsn_tolerance:
        return [
            R.SEQ_XVAR_DSN_MISMATCH.raise_finding(
                f"Discontinuity sequence numbers span {spread} across the ladder at "
                f"{at.isoformat()}, against a tolerance of "
                f"{thresholds.cross_variant_dsn_tolerance}: {dsns}. The Tizen player holds one "
                "counter for every rendition.",
                evidence=evidence,
                stream_layer=layer,
                at=at,
            )
        ]

    for msn in sorted(comparable_msns(snapshots)):
        anchored = {s.variant: _dsn_at_msn(s, msn) for s in snapshots}
        present = {k: v for k, v in anchored.items() if v is not None}
        if len(present) < 2 or len(set(present.values())) <= 1:
            continue
        return [
            R.SEQ_XVAR_DSN_MISMATCH.raise_finding(
                f"Media sequence {msn} is listed by every rendition and carries a different "
                f"discontinuity sequence on each: {present}. The Tizen player holds one counter "
                "for every rendition.",
                evidence={**evidence, "anchor_msn": msn, "dsn_at_anchor": present},
                stream_layer=layer,
                at=at,
            )
        ]
    return []


def _check_common_msn_alignment(
    video: list[VariantSnapshot], *, layer: StreamLayer, at: dt.datetime, tolerance_s: float
) -> list[Finding]:
    """Every rung must cover the same span of the timeline for the same MSN.

    Durations are compared within `tolerance_s`: two encoders write the same segment as 6.006
    and 6.0 s, and a rung cut on audio-frame boundaries lands a few milliseconds off.
    """
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
        if durations and max(durations.values()) - min(durations.values()) > tolerance_s:
            findings.append(
                R.SEQ_XVAR_SEGMENT_MISMATCH.raise_finding(
                    f"Media sequence {msn} declares durations across the ladder that differ by "
                    f"{max(durations.values()) - min(durations.values()):.3f} s, against a "
                    f"tolerance of {tolerance_s} s: {durations}.",
                    evidence={"msn": msn, "durations": durations, "tolerance_s": tolerance_s},
                    stream_layer=layer,
                    at=at,
                )
            )
            break
    return findings


def _check_discontinuity_positions(
    video: list[VariantSnapshot], *, layer: StreamLayer, at: dt.datetime
) -> list[Finding]:
    """Discontinuity tags compared only where every rung publishes the same segment.

    Comparing whole windows reported the ladder every time one rung had slid and another had
    not: a tag is dropped from a segment the moment that segment becomes the first in the
    window, because the count moves into EXT-X-DISCONTINUITY-SEQUENCE. Restricting the
    comparison to the media sequence numbers every rung lists, first segment excluded, leaves
    only tags that genuinely sit at different positions.
    """
    findings: list[Finding] = []
    if len(video) < 2:
        return findings

    comparable = comparable_msns(video)
    if not comparable:
        return findings

    positions = {
        s.variant: sorted(
            seg.msn
            for seg in s.playlist.segments
            if seg.discontinuity_before and seg.msn in comparable
        )
        for s in video
    }
    if len({tuple(v) for v in positions.values()}) <= 1:
        return findings

    window = f"{min(comparable)}-{max(comparable)}"
    findings.append(
        R.SEQ_XVAR_DISC_POSITION.raise_finding(
            f"Across media sequence {window}, which every rung lists, discontinuity tags sit "
            f"at different positions: {positions}.",
            evidence={
                "positions": positions,
                "comparable_msn_from": min(comparable),
                "comparable_msn_to": max(comparable),
                "polled_at": {s.variant: s.at.isoformat() for s in video},
                "at": at.isoformat(),
            },
            stream_layer=layer,
            at=at,
        )
    )
    return findings


def _pdt_at(playlist: MediaPlaylist, msn: int) -> dt.datetime | None:
    """The program date time of segment `msn`, carried forward from the last tag before it."""
    from app.analysis.rules.media_playlist import parse_pdt

    anchor: dt.datetime | None = None
    for segment in playlist.segments:
        if segment.program_date_time:
            anchor = parse_pdt(segment.program_date_time) or anchor
        if segment.msn == msn:
            return anchor
        if anchor is not None:
            anchor += dt.timedelta(seconds=segment.duration)
    return None


def _check_pdt_alignment(
    snapshots: list[VariantSnapshot], *, layer: StreamLayer, at: dt.datetime, tolerance_s: float
) -> list[Finding]:
    """Program date times compared at one media sequence number every rendition lists.

    Comparing each rendition's first segment reported a difference of one whole segment
    every time one rung's window had slid and the next had not.
    """
    findings: list[Finding] = []
    common = comparable_msns(snapshots)
    if not common:
        return findings
    anchor_msn = max(common)
    stamps: dict[str, dt.datetime] = {}
    for snapshot in snapshots:
        moment = _pdt_at(snapshot.playlist, anchor_msn)
        if moment is not None:
            stamps[snapshot.variant] = moment
    if len(stamps) < 2:
        return findings
    spread = (max(stamps.values()) - min(stamps.values())).total_seconds()
    if spread > tolerance_s:
        findings.append(
            R.SEQ_XVAR_PDT.raise_finding(
                f"Media sequence {anchor_msn}, listed by every rendition, carries program date "
                f"times {spread:.2f} s apart against a tolerance of {tolerance_s} s: "
                + ", ".join(f"{k} at {v.isoformat()}" for k, v in sorted(stamps.items()))
                + ".",
                evidence={
                    "anchor_msn": anchor_msn,
                    "pdt_by_variant": {k: v.isoformat() for k, v in stamps.items()},
                    "spread_s": spread,
                    "tolerance_s": tolerance_s,
                },
                stream_layer=layer,
                at=at,
            )
        )
    return findings
