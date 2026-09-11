"""SSAI and ad-splice detectors."""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.analysis.rules import catalogue as R
from app.analysis.rules.base import Finding, StreamLayer
from app.config import Thresholds
from app.hls.playlist import MediaPlaylist
from app.hls.scte35 import CueWindow, SpliceInfo, extract_cue_windows
from app.hls.uri import host_of

CUE_DURATION_TOLERANCE_S = 1.0


def check_cue_windows(
    playlist: MediaPlaylist,
    *,
    variant: str,
    layer: StreamLayer,
    thresholds: Thresholds,
) -> list[Finding]:
    findings: list[Finding] = []
    windows = extract_cue_windows(playlist)
    target = playlist.target_duration or 0.0

    for window in windows:
        evidence: dict[str, Any] = {
            "variant": variant,
            "start_msn": window.start_msn,
            "end_msn": window.end_msn,
            "declared_duration_s": window.declared_duration_s,
            "delivered_duration_s": window.delivered_duration_s,
            "segment_count": window.segment_count,
        }
        if not window.closed:
            findings.append(
                R.ADS_CUE_UNBALANCED.raise_finding(
                    f"{variant} opened EXT-X-CUE-OUT at media sequence {window.start_msn} with "
                    f"DURATION={window.declared_duration_s} and the window ends without an "
                    "EXT-X-CUE-IN.",
                    evidence=evidence,
                    stream_layer=layer,
                    variant=variant,
                )
            )
            continue

        delta = window.duration_delta_s
        if delta is not None and abs(delta) > CUE_DURATION_TOLERANCE_S:
            findings.append(
                R.ADS_DURATION_MISMATCH.raise_finding(
                    f"The break at media sequence {window.start_msn} on {variant} declares "
                    f"{window.declared_duration_s:.1f} s and delivers "
                    f"{window.delivered_duration_s:.1f} s across {window.segment_count} "
                    f"segment(s), a difference of {delta:+.1f} s.",
                    evidence=evidence,
                    stream_layer=layer,
                    variant=variant,
                )
            )

        if not window.discontinuity_at_out or not window.discontinuity_at_in:
            missing = []
            if not window.discontinuity_at_out:
                missing.append("ad in")
            if not window.discontinuity_at_in:
                missing.append("ad out")
            findings.append(
                R.ADS_NO_DISCONTINUITY.raise_finding(
                    f"The break at media sequence {window.start_msn} on {variant} carries no "
                    f"EXT-X-DISCONTINUITY at {' and no EXT-X-DISCONTINUITY at '.join(missing)}.",
                    evidence=evidence,
                    stream_layer=layer,
                    variant=variant,
                )
            )

        if target:
            for segment in playlist.segments:
                in_break = window.start_msn <= segment.msn <= (window.end_msn or window.start_msn)
                if in_break and segment.duration > target:
                    findings.append(
                        R.ADS_EXTINF_OVER_TD.raise_finding(
                            f"Ad segment {segment.msn} on {variant} declares EXTINF "
                            f"{segment.duration:.3f} s against a channel target duration of "
                            f"{target:.0f} s.",
                            evidence={
                                **evidence,
                                "msn": segment.msn,
                                "extinf": segment.duration,
                            },
                            stream_layer=layer,
                            variant=variant,
                        )
                    )
                    break

    if windows and not any(f.severity.rank >= 2 for f in findings):
        findings.append(
            R.ADS_OK.raise_finding(
                f"{variant} carries {len(windows)} ad break(s); each opened and closed with "
                "matching cues and delivered its declared duration.",
                evidence={"variant": variant, "break_count": len(windows)},
                stream_layer=layer,
                variant=variant,
            )
        )
    return findings


def check_cue_agreement(
    *,
    inband: list[SpliceInfo],
    playlist_windows: list[CueWindow],
    variant: str,
    layer: StreamLayer,
    at: dt.datetime | None = None,
) -> list[Finding]:
    """In-band SCTE-35 against the playlist cues."""
    findings: list[Finding] = []
    moment = at or dt.datetime.now(dt.UTC)
    inband_starts = [info for info in inband if info.is_ad_start]

    if inband_starts and not playlist_windows:
        findings.append(
            R.ADS_CUE_MISSING_IN_MANIFEST.raise_finding(
                f"{variant} carries {len(inband_starts)} in-band SCTE-35 splice(s) "
                f"({[info.command_name for info in inband_starts]}) and the playlist advertises "
                "no EXT-X-CUE-OUT or EXT-X-DATERANGE.",
                evidence={
                    "variant": variant,
                    "inband": [info.as_dict() for info in inband_starts[:5]],
                },
                stream_layer=layer,
                variant=variant,
                at=moment,
            )
        )
    elif playlist_windows and not inband_starts:
        findings.append(
            R.ADS_CUE_MISSING_INBAND.raise_finding(
                f"{variant} advertises {len(playlist_windows)} playlist cue(s) and the transport "
                "stream carries no SCTE-35 splice.",
                evidence={"variant": variant, "playlist_cues": len(playlist_windows)},
                stream_layer=layer,
                variant=variant,
                at=moment,
            )
        )

    for info, window in zip(inband_starts, playlist_windows, strict=False):
        if info.break_duration_s is None or window.declared_duration_s is None:
            continue
        delta = window.declared_duration_s - info.break_duration_s
        if abs(delta) > CUE_DURATION_TOLERANCE_S:
            findings.append(
                R.ADS_CUE_DURATION.raise_finding(
                    f"The SCTE-35 {info.command_name} declares a break of "
                    f"{info.break_duration_s:.1f} s and the playlist cue at media sequence "
                    f"{window.start_msn} declares {window.declared_duration_s:.1f} s, a "
                    f"difference of {delta:+.1f} s.",
                    evidence={
                        "variant": variant,
                        "inband": info.as_dict(),
                        "start_msn": window.start_msn,
                        "declared_duration_s": window.declared_duration_s,
                    },
                    stream_layer=layer,
                    variant=variant,
                    at=moment,
                )
            )
    return findings


def check_break_across_rungs(
    windows_by_variant: dict[str, list[CueWindow]], *, layer: StreamLayer
) -> list[Finding]:
    """Every rung must leave a break with the same discontinuity count."""
    findings: list[Finding] = []
    if len(windows_by_variant) < 2:
        return findings

    counts = {
        variant: sum(
            (1 if w.discontinuity_at_out else 0) + (1 if w.discontinuity_at_in else 0)
            for w in windows
        )
        for variant, windows in windows_by_variant.items()
    }
    if len(set(counts.values())) > 1:
        findings.append(
            R.ADS_DSN_DIVERGENCE.raise_finding(
                f"The rungs emit different numbers of discontinuities across their ad breaks: "
                f"{counts}.",
                evidence={"discontinuities_by_variant": counts},
                stream_layer=layer,
            )
        )

    starts = {
        variant: sorted(w.start_msn for w in windows)
        for variant, windows in windows_by_variant.items()
    }
    if len({tuple(v) for v in starts.values()}) > 1:
        findings.append(
            R.ADS_NO_DISCONTINUITY.raise_finding(
                f"Ad breaks open at different media sequence numbers across the ladder: {starts}.",
                evidence={"break_starts": starts},
                stream_layer=layer,
            )
        )
    return findings


def check_ad_delivery(
    *,
    content_host_p95_ms: float,
    ad_host_p95_ms: float,
    ad_host: str,
    content_host: str,
    variant: str,
    layer: StreamLayer,
    factor: float = 1.5,
) -> Finding | None:
    if content_host_p95_ms <= 0 or ad_host_p95_ms <= content_host_p95_ms * factor:
        return None
    return R.ADS_HOST_SLOW.raise_finding(
        f"Ad segments from {ad_host} return their first byte at a 95th percentile of "
        f"{ad_host_p95_ms:.0f} ms while content segments from {content_host} return at "
        f"{content_host_p95_ms:.0f} ms.",
        evidence={
            "ad_host": ad_host,
            "content_host": content_host,
            "ad_p95_ms": ad_host_p95_ms,
            "content_p95_ms": content_host_p95_ms,
        },
        stream_layer=layer,
        variant=variant,
    )


def check_creative_configuration(
    *,
    content_sps: dict[str, Any] | None,
    ad_sps: dict[str, Any] | None,
    content_audio: dict[str, Any] | None,
    ad_audio: dict[str, Any] | None,
    variant: str,
    layer: StreamLayer,
    msn: int,
) -> Finding | None:
    """A creative encoded differently from the rung it is spliced into."""
    differences: dict[str, list[Any]] = {}
    for key in ("resolution", "profile", "level", "max_num_ref_frames"):
        before = (content_sps or {}).get(key)
        after = (ad_sps or {}).get(key)
        if before is not None and after is not None and before != after:
            differences[key] = [before, after]
    for key in ("sample_rate", "channel_config", "aot"):
        before = (content_audio or {}).get(key)
        after = (ad_audio or {}).get(key)
        if before is not None and after is not None and before != after:
            differences[f"audio_{key}"] = [before, after]

    if not differences:
        return None
    return R.ADS_CREATIVE_MISMATCH.raise_finding(
        f"The creative spliced at media sequence {msn} on {variant} differs from the content: "
        + ", ".join(f"{k} {v[0]} -> {v[1]}" for k, v in differences.items())
        + ".",
        evidence={"variant": variant, "msn": msn, "differences": differences},
        stream_layer=layer,
        variant=variant,
    )


def ad_host_of(uri: str, content_host: str) -> str | None:
    """The host of an ad segment, when it differs from the content host."""
    host = host_of(uri)
    return host if host and host != content_host else None
