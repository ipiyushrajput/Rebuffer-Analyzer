"""Segment, video bitstream, audio and A/V-sync detectors.

`check_segment` judges a segment on its own. `check_segment_pair` judges it against the
previous segment of the same rung — that is where timestamp continuity, decoder
reconfiguration and skew drift are found.
"""

from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass, field
from typing import Any

from app.analysis.rules import catalogue as R
from app.analysis.rules.base import Finding, StreamLayer
from app.config import Thresholds
from app.media.segment import SegmentAnalysis
from app.media.ts import PTS_HZ, pts_diff


@dataclass(slots=True)
class RungHistory:
    """Per-rung state carried between segments so pair checks have something to compare."""

    variant: str
    last: SegmentAnalysis | None = None
    last_discontinuity_msn: int | None = None
    skews: list[tuple[int, float]] = field(default_factory=list)
    keyframe_msns: set[int] = field(default_factory=set)
    sps_by_msn: dict[int, dict[str, Any]] = field(default_factory=dict)
    # Boundaries where the two segments sampled either side were not consecutive, so the
    # timestamp continuity checks had nothing contiguous to measure.
    skipped_boundaries: int = 0


def check_segment(
    analysis: SegmentAnalysis,
    *,
    variant: str,
    layer: StreamLayer,
    thresholds: Thresholds,
    declared_bandwidth: int | None = None,
    is_audio_only: bool = False,
    at: dt.datetime | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    moment = at or dt.datetime.now(dt.UTC)
    evidence: dict[str, Any] = {
        "variant": variant,
        "msn": analysis.msn,
        "uri": analysis.uri,
        "bytes": analysis.byte_size,
        "container": analysis.container,
        "extinf": analysis.declared_duration,
        "actual_duration": analysis.actual_duration,
    }

    def raise_for(rule: Any, detail: str, extra: dict[str, Any] | None = None) -> None:
        findings.append(
            rule.raise_finding(
                detail,
                evidence={**evidence, **(extra or {})},
                stream_layer=layer,
                variant=variant,
                at=moment,
            )
        )

    if analysis.encrypted:
        raise_for(
            R.SKIP_ENCRYPTED,
            f"Segment {analysis.msn} on {variant} is encrypted and no supplied key decrypts it, "
            "so the video, audio and A/V checks did not run on it.",
        )
        return findings

    if analysis.byte_size == 0:
        raise_for(R.SEG_ZERO, f"Segment {analysis.msn} on {variant} returned a zero-byte body.")
        return findings

    if analysis.container == "unknown":
        raise_for(
            R.SEG_CONTAINER_UNKNOWN,
            f"Segment {analysis.msn} on {variant} starts with bytes matching no known container "
            f"and its URI carries no recognised extension: {analysis.uri}.",
        )
        return findings

    if analysis.byte_size <= thresholds.tiny_segment_bytes:
        raise_for(
            R.SEG_TINY,
            f"Segment {analysis.msn} on {variant} is {analysis.byte_size} bytes against a "
            f"threshold of {thresholds.tiny_segment_bytes} bytes, while declaring "
            f"{analysis.declared_duration} s.",
        )

    if analysis.container == "ts":
        if analysis.parse_error and "sync byte" in analysis.parse_error:
            raise_for(R.SEG_SYNC, f"Segment {analysis.msn} on {variant}: {analysis.parse_error}")
        if analysis.pat_present is False or analysis.pmt_present is False:
            missing = []
            if analysis.pat_present is False:
                missing.append("PAT")
            if analysis.pmt_present is False:
                missing.append("PMT")
            raise_for(
                R.SEG_NO_PAT_PMT,
                f"Segment {analysis.msn} on {variant} carries no {' and no '.join(missing)}.",
            )
        if analysis.continuity_errors:
            raise_for(
                R.SEG_CONTINUITY,
                f"Segment {analysis.msn} on {variant} has {analysis.continuity_errors} "
                "continuity-counter discontinuity(ies).",
                {"continuity_errors": analysis.continuity_errors},
            )

    if analysis.container == "aac" and analysis.id3_timestamp is None:
        raise_for(
            R.SEG_NO_ID3,
            f"Segment {analysis.msn} on {variant} is packed audio and carries no "
            "com.apple.streaming.transportStreamTimestamp ID3 PRIV frame.",
        )

    if analysis.container == "fmp4" and analysis.parse_error == "fMP4 segment carries no moof box":
        raise_for(
            R.SEG_BAD_INIT,
            f"Segment {analysis.msn} on {variant} carries no moof box, so it holds no media "
            "fragment.",
        )

    delta = analysis.duration_delta_s
    if delta is not None and abs(delta) > thresholds.extinf_vs_actual_tolerance_s:
        raise_for(
            R.SEG_DURATION_MISMATCH,
            f"Segment {analysis.msn} on {variant} declares EXTINF "
            f"{analysis.declared_duration:.3f} s and carries {analysis.actual_duration:.3f} s of "
            f"media, a difference of {delta:+.3f} s against a tolerance of "
            f"{thresholds.extinf_vs_actual_tolerance_s} s.",
            {"duration_delta_s": delta},
        )

    measured = analysis.measured_bitrate_bps
    if measured and declared_bandwidth:
        limit = declared_bandwidth * (1 + thresholds.bandwidth_overshoot_tolerance)
        if measured > limit:
            raise_for(
                R.SEG_BITRATE_OVER_DECLARED,
                f"Segment {analysis.msn} on {variant} is {analysis.byte_size} bytes over "
                f"{analysis.actual_duration or analysis.declared_duration:.3f} s, which is "
                f"{measured / 1000:.0f} kbit/s against a declared BANDWIDTH of "
                f"{declared_bandwidth // 1000} kbit/s "
                f"(+{thresholds.bandwidth_overshoot_tolerance:.0%} tolerance).",
                {"measured_bps": measured, "declared_bandwidth": declared_bandwidth},
            )

    findings += _check_video(analysis, variant=variant, layer=layer, at=moment, evidence=evidence)
    findings += _check_audio(
        analysis,
        variant=variant,
        layer=layer,
        at=moment,
        evidence=evidence,
        is_audio_only=is_audio_only,
    )
    findings += _check_av(
        analysis, variant=variant, layer=layer, thresholds=thresholds, at=moment, evidence=evidence
    )
    return findings


def _check_video(
    analysis: SegmentAnalysis,
    *,
    variant: str,
    layer: StreamLayer,
    at: dt.datetime,
    evidence: dict[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    if not analysis.has_video:
        return findings

    if analysis.starts_with_keyframe is False:
        findings.append(
            R.VID_NO_KEYFRAME.raise_finding(
                f"Segment {analysis.msn} on {variant} opens on a non-random-access picture.",
                evidence={**evidence, "sps": analysis.sps},
                stream_layer=layer,
                variant=variant,
                at=at,
            )
        )

    if analysis.has_sps is False or analysis.has_pps is False:
        missing = [
            name
            for name, present in (("SPS", analysis.has_sps), ("PPS", analysis.has_pps))
            if present is False
        ]
        findings.append(
            R.VID_NO_SPS.raise_finding(
                f"Segment {analysis.msn} on {variant} carries no {' and no '.join(missing)}.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant,
                at=at,
            )
        )

    if analysis.video_codec == "hevc" and analysis.has_vps is False:
        findings.append(
            R.VID_NO_VPS.raise_finding(
                f"Segment {analysis.msn} on {variant} is HEVC and carries no VPS.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant,
                at=at,
            )
        )

    sps = analysis.sps or {}
    if sps.get("scan_type") == "interlaced":
        findings.append(
            R.VID_INTERLACED.raise_finding(
                f"{variant} codes interlaced video at {sps.get('resolution')}.",
                evidence={**evidence, "sps": sps},
                stream_layer=layer,
                variant=variant,
                at=at,
            )
        )
    return findings


def _check_audio(
    analysis: SegmentAnalysis,
    *,
    variant: str,
    layer: StreamLayer,
    at: dt.datetime,
    evidence: dict[str, Any],
    is_audio_only: bool,
) -> list[Finding]:
    findings: list[Finding] = []
    if analysis.has_video and not analysis.has_audio and not is_audio_only:
        findings.append(
            R.AUD_MISSING_IN_MUXED.raise_finding(
                f"Segment {analysis.msn} on {variant} carries video and no audio elementary "
                "stream.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant,
                at=at,
            )
        )
    return findings


def _check_av(
    analysis: SegmentAnalysis,
    *,
    variant: str,
    layer: StreamLayer,
    thresholds: Thresholds,
    at: dt.datetime,
    evidence: dict[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    skew = analysis.av_skew_ms
    if skew is None:
        return findings

    magnitude = abs(skew)
    if magnitude >= thresholds.av_pts_delta_critical_ms:
        findings.append(
            R.AV_DELTA_CRITICAL.raise_finding(
                f"Segment {analysis.msn} on {variant} starts audio {skew:+.0f} ms from video "
                f"against a critical threshold of {thresholds.av_pts_delta_critical_ms} ms.",
                evidence={**evidence, "av_skew_ms": skew},
                stream_layer=layer,
                variant=variant,
                at=at,
            )
        )
    elif magnitude > thresholds.av_skew_error_ms:
        findings.append(
            R.AV_SKEW.raise_finding(
                f"Segment {analysis.msn} on {variant} starts audio {skew:+.0f} ms from video "
                f"against an error threshold of {thresholds.av_skew_error_ms} ms "
                f"(normal is under {thresholds.av_skew_normal_ms} ms).",
                evidence={**evidence, "av_skew_ms": skew},
                stream_layer=layer,
                variant=variant,
                at=at,
            )
        )
    return findings


def check_segment_pair(
    history: RungHistory,
    current: SegmentAnalysis,
    *,
    layer: StreamLayer,
    thresholds: Thresholds,
    discontinuity_before: bool,
    at: dt.datetime | None = None,
) -> list[Finding]:
    """Checks that need the previous segment of the same rung."""
    findings: list[Finding] = []
    moment = at or dt.datetime.now(dt.UTC)
    previous = history.last
    variant = history.variant

    if previous is None or current.encrypted or previous.encrypted:
        history.last = current
        return findings

    # How many segments the stream published between the two that were sampled. Zero means
    # this boundary was observed; anything else means it was not.
    skipped = current.msn - previous.msn - 1 if current.msn >= 0 and previous.msn >= 0 else 0
    adjacent = skipped == 0

    evidence: dict[str, Any] = {
        "variant": variant,
        "previous_msn": previous.msn,
        "current_msn": current.msn,
        "previous_uri": previous.uri,
        "current_uri": current.uri,
        "segments_skipped": skipped,
    }

    if not adjacent:
        history.skipped_boundaries += 1
        findings.append(
            R.SKIP_NONCONSECUTIVE.raise_finding(
                f"Segments {previous.msn} and {current.msn} on {variant} are not consecutive, "
                f"so the timestamp continuity checks did not run at this boundary: "
                f"{skipped} segment(s) were published between them and not sampled.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant,
                at=moment,
            )
        )

    findings += _check_pts_continuity(
        previous,
        current,
        variant=variant,
        layer=layer,
        thresholds=thresholds,
        discontinuity_before=discontinuity_before,
        adjacent=adjacent,
        at=moment,
        evidence=evidence,
    )

    # An AAC configuration change forces a decoder reconfiguration mid-playback.
    before = previous.aac_config or {}
    after = current.aac_config or {}
    if before and after:
        keys = ("aot", "sample_rate", "channel_config")
        changed = {k: (before.get(k), after.get(k)) for k in keys if before.get(k) != after.get(k)}
        if changed:
            findings.append(
                R.AUD_CONFIG_CHANGED.raise_finding(
                    f"Audio configuration changed between segment {previous.msn} and "
                    f"{current.msn} on {variant}: "
                    + ", ".join(f"{k} {v[0]} -> {v[1]}" for k, v in changed.items())
                    + ".",
                    evidence={**evidence, "changed": {k: list(v) for k, v in changed.items()}},
                    stream_layer=layer,
                    variant=variant,
                    at=moment,
                )
            )

    before_ac3 = previous.ac3_config or {}
    after_ac3 = current.ac3_config or {}
    if before_ac3 and after_ac3:
        keys = ("codec", "sample_rate", "channels")
        changed = {
            k: (before_ac3.get(k), after_ac3.get(k))
            for k in keys
            if before_ac3.get(k) != after_ac3.get(k)
        }
        if changed:
            findings.append(
                R.AUD_AC3_INCONSISTENT.raise_finding(
                    f"Dolby audio parameters changed between segment {previous.msn} and "
                    f"{current.msn} on {variant}: "
                    + ", ".join(f"{k} {v[0]} -> {v[1]}" for k, v in changed.items())
                    + ".",
                    evidence={**evidence, "changed": {k: list(v) for k, v in changed.items()}},
                    stream_layer=layer,
                    variant=variant,
                    at=moment,
                )
            )

    before_sps = previous.sps or {}
    after_sps = current.sps or {}
    if before_sps and after_sps:
        sps_keys = ("resolution", "profile", "level", "max_num_ref_frames", "chroma_format")
        changed = {
            k: (before_sps.get(k), after_sps.get(k))
            for k in sps_keys
            if before_sps.get(k) != after_sps.get(k)
        }
        if changed:
            rule = R.VID_CONFIG_AT_DISC if discontinuity_before else R.VID_SPS_CHANGED
            findings.append(
                rule.raise_finding(
                    f"The sequence parameter set changed between segment {previous.msn} and "
                    f"{current.msn} on {variant}"
                    + (" across a discontinuity" if discontinuity_before else "")
                    + ": "
                    + ", ".join(f"{k} {v[0]} -> {v[1]}" for k, v in changed.items())
                    + ".",
                    evidence={**evidence, "changed": {k: list(v) for k, v in changed.items()}},
                    stream_layer=layer,
                    variant=variant,
                    at=moment,
                )
            )

    findings += _check_skew_drift(
        history, current, layer=layer, thresholds=thresholds, at=moment, evidence=evidence
    )

    history.last = current
    if current.sps:
        history.sps_by_msn[current.msn] = current.sps
    if current.starts_with_keyframe:
        history.keyframe_msns.add(current.msn)
    return findings


def _check_pts_continuity(
    previous: SegmentAnalysis,
    current: SegmentAnalysis,
    *,
    variant: str,
    layer: StreamLayer,
    thresholds: Thresholds,
    discontinuity_before: bool,
    adjacent: bool,
    at: dt.datetime,
    evidence: dict[str, Any],
) -> list[Finding]:
    """Timestamp contiguity across one segment boundary.

    Every rule here asserts something about where one segment ends and the **next** begins, so
    all of them need the two segments to be consecutive. They are not always: a rung sampled
    every Nth segment never sees an adjacent pair, and a full rung misses one whenever a
    segment rolls out of the live window between polls. Measured across such a skip, the
    missing segment's own duration reads as a gap of exactly that length and the rule fires on
    a stream that is perfectly contiguous. So the boundary is checked only when it was
    observed; when it was not, `SKIP_NONCONSECUTIVE` says so and these rules stay silent.
    """
    findings: list[Finding] = []
    if not adjacent:
        return findings
    end = previous.video_last_pts if previous.has_video else previous.audio_last_pts
    start = current.video_first_pts if current.has_video else current.audio_first_pts
    if end is None or start is None:
        return findings

    gap_ticks = pts_diff(start, end)
    gap_ms = gap_ticks / PTS_HZ * 1000.0
    tolerance = thresholds.pts_gap_tolerance_ms
    pts_evidence = {
        **evidence,
        "previous_last_pts": end,
        "current_first_pts": start,
        "gap_ms": gap_ms,
        "tolerance_ms": tolerance,
        "discontinuity_declared": discontinuity_before,
    }

    if discontinuity_before:
        return findings

    if gap_ms < -tolerance:
        # A large backwards step is a timestamp base reset, not an overlap.
        if abs(gap_ms) > 1000:
            findings.append(
                R.SEG_PTS_RESET.raise_finding(
                    f"Segment {current.msn} on {variant} starts at PTS {start} after segment "
                    f"{previous.msn} ended at {end}, a step of {gap_ms:.0f} ms, and the playlist "
                    "declares no discontinuity at this boundary.",
                    evidence=pts_evidence,
                    stream_layer=layer,
                    variant=variant,
                    at=at,
                )
            )
        else:
            findings.append(
                R.SEG_PTS_OVERLAP.raise_finding(
                    f"Segment {current.msn} on {variant} starts {abs(gap_ms):.0f} ms before "
                    f"segment {previous.msn} ends, against a tolerance of {tolerance} ms.",
                    evidence=pts_evidence,
                    stream_layer=layer,
                    variant=variant,
                    at=at,
                )
            )
    elif gap_ms > tolerance:
        findings.append(
            R.SEG_PTS_GAP.raise_finding(
                f"Segment {current.msn} on {variant} starts {gap_ms:.0f} ms after segment "
                f"{previous.msn} ends, against a tolerance of {tolerance} ms, and the playlist "
                "declares no discontinuity at this boundary.",
                evidence=pts_evidence,
                stream_layer=layer,
                variant=variant,
                at=at,
            )
        )

    # A rollover that lands in one track and not the other separates them by a counter period.
    if previous.has_video and previous.has_audio and current.has_video and current.has_audio:
        video_wrapped = _wrapped(previous.video_last_pts, current.video_first_pts)
        audio_wrapped = _wrapped(previous.audio_last_pts, current.audio_first_pts)
        if video_wrapped != audio_wrapped:
            findings.append(
                R.AV_ROLLOVER_SPLIT.raise_finding(
                    f"The 33-bit presentation timestamp wrapped on the "
                    f"{'video' if video_wrapped else 'audio'} track and not on the "
                    f"{'audio' if video_wrapped else 'video'} track between segment "
                    f"{previous.msn} and {current.msn} on {variant}.",
                    evidence=pts_evidence,
                    stream_layer=layer,
                    variant=variant,
                    at=at,
                )
            )
    return findings


def _wrapped(end: int | None, start: int | None) -> bool:
    if end is None or start is None:
        return False
    return start < end and (end - start) > (1 << 32)


def _check_skew_drift(
    history: RungHistory,
    current: SegmentAnalysis,
    *,
    layer: StreamLayer,
    thresholds: Thresholds,
    at: dt.datetime,
    evidence: dict[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    if current.av_skew_ms is None:
        return findings
    history.skews.append((current.msn, current.av_skew_ms))
    if len(history.skews) < 4:
        return findings

    window = history.skews[-6:]
    first = window[0][1]
    last = window[-1][1]
    growth = abs(last) - abs(first)
    monotonic = all(abs(b[1]) >= abs(a[1]) - 1.0 for a, b in itertools.pairwise(window))
    if monotonic and growth > thresholds.av_skew_normal_ms:
        findings.append(
            R.AV_SKEW_DRIFT.raise_finding(
                f"A/V skew on {history.variant} grew from {first:+.0f} ms at segment "
                f"{window[0][0]} to {last:+.0f} ms at segment {window[-1][0]}, "
                f"{growth:+.0f} ms across {len(window)} consecutive segments.",
                evidence={**evidence, "skew_window": [list(s) for s in window]},
                stream_layer=layer,
                variant=history.variant,
                at=at,
            )
        )
    return findings


def check_keyframe_alignment(
    histories: dict[str, RungHistory], *, layer: StreamLayer
) -> list[Finding]:
    """Every rung must place a random access point at the same media sequence numbers."""
    findings: list[Finding] = []
    if len(histories) < 2:
        return findings
    sets = {
        name: history.keyframe_msns for name, history in histories.items() if history.keyframe_msns
    }
    if len(sets) < 2:
        return findings
    common = set.intersection(*sets.values())
    union = set.union(*sets.values())
    missing = union - common
    if missing:
        detail = {name: sorted(missing - msns)[:5] for name, msns in sets.items() if missing - msns}
        findings.append(
            R.VID_KEYFRAME_MISALIGNED.raise_finding(
                f"{len(missing)} media sequence number(s) start on a random access point on some "
                f"rungs and not on others: {detail}.",
                evidence={"missing_by_variant": dict(detail.items())},
                stream_layer=layer,
            )
        )
    return findings


def check_dpb_across_rungs(
    sps_by_variant: dict[str, dict[str, Any]], *, layer: StreamLayer
) -> Finding | None:
    """The Tizen DPB rule: rungs that disagree on max_num_ref_frames force a realloc."""
    counts = {
        variant: sps.get("max_num_ref_frames")
        for variant, sps in sps_by_variant.items()
        if sps.get("max_num_ref_frames") is not None
    }
    if len(set(counts.values())) <= 1:
        return None
    footprints = {variant: sps.get("dpb_footprint") for variant, sps in sps_by_variant.items()}
    return R.VID_DPB_MISMATCH.raise_finding(
        "The ladder declares different reference frame counts: "
        + ", ".join(
            f"{variant} at {sps_by_variant[variant].get('resolution')} uses "
            f"max_num_ref_frames={count}"
            for variant, count in sorted(counts.items())
        )
        + ". Every ABR switch between these rungs reallocates the decoded picture buffer.",
        evidence={"max_num_ref_frames": counts, "dpb_footprint": footprints},
        stream_layer=layer,
    )


def check_demuxed_audio_coverage(
    *,
    video_ranges: list[tuple[int, float, float]],
    audio_ranges: list[tuple[int, float, float]],
    variant: str,
    layer: StreamLayer,
) -> list[Finding]:
    """Every video segment's time range must be covered by an audio segment (DemuxAnalyzer).

    Ranges are ``(msn, start_s, end_s)`` on a shared timeline. Pairing is by overlap, which
    survives audio and video being segmented on different boundaries.
    """
    findings: list[Finding] = []
    if not video_ranges or not audio_ranges:
        return findings

    for msn, start, end in video_ranges:
        covered = any(a_start < end and a_end > start for _, a_start, a_end in audio_ranges)
        if not covered:
            findings.append(
                R.AUD_SEGMENT_MISSING.raise_finding(
                    f"Video segment {msn} on {variant} covers {start:.3f}–{end:.3f} s and no "
                    f"audio segment overlaps that range. The audio rendition spans "
                    f"{audio_ranges[0][1]:.3f}–{audio_ranges[-1][2]:.3f} s.",
                    evidence={
                        "variant": variant,
                        "video_msn": msn,
                        "video_range": [start, end],
                        "audio_span": [audio_ranges[0][1], audio_ranges[-1][2]],
                    },
                    stream_layer=layer,
                    variant=variant,
                )
            )
            break

    video_total = sum(end - start for _, start, end in video_ranges)
    audio_total = sum(end - start for _, start, end in audio_ranges)
    drift = abs(video_total - audio_total)
    if video_total > 0 and drift > max(0.5, video_total * 0.02):
        findings.append(
            R.AUD_DURATION_DRIFT.raise_finding(
                f"{variant} carries {video_total:.2f} s of video and {audio_total:.2f} s of "
                f"audio across the same window, a drift of {drift:.2f} s.",
                evidence={
                    "variant": variant,
                    "video_total_s": video_total,
                    "audio_total_s": audio_total,
                    "drift_s": drift,
                },
                stream_layer=layer,
                variant=variant,
            )
        )
    return findings
