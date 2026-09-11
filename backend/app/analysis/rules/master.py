"""Master playlist detectors: conformance, ladder shape, and cross-level consistency."""

from __future__ import annotations

import itertools
from typing import Any

from app.analysis.rules import catalogue as R
from app.analysis.rules.base import Finding, StreamLayer
from app.config import Thresholds
from app.hls.playlist import MasterPlaylist, Variant

# Tags whose presence requires at least this EXT-X-VERSION.
TAG_MIN_VERSION = {
    "#EXT-X-KEY:METHOD=SAMPLE-AES": 5,
    "#EXT-X-MAP": 5,
    "#EXT-X-BYTERANGE": 4,
    "#EXT-X-I-FRAMES-ONLY": 4,
    "#EXT-X-INDEPENDENT-SEGMENTS": 6,
    "#EXT-X-GAP": 8,
    "#EXT-X-DATERANGE": 4,
}

SUPPORTED_FRAME_RATES = (29.97, 30.0, 25.0, 50.0, 59.94, 60.0)
VALIDATED_FRAME_RATES = (29.97, 30.0)


def check_master(
    master: MasterPlaylist, *, layer: StreamLayer, thresholds: Thresholds
) -> list[Finding]:
    findings: list[Finding] = []
    evidence_base: dict[str, Any] = {"url": master.final_url}

    if master.first_line != "#EXTM3U":
        findings.append(
            R.MST_NO_EXTM3U.raise_finding(
                f"{master.final_url} starts with {master.first_line!r}.",
                evidence={**evidence_base, "first_line": master.first_line},
                stream_layer=layer,
            )
        )

    required_version = _required_version(master.raw)
    if master.version is not None and master.version < required_version:
        findings.append(
            R.MST_VERSION_LOW.raise_finding(
                f"{master.final_url} declares EXT-X-VERSION:{master.version} and uses tags that "
                f"require version {required_version}.",
                evidence={
                    **evidence_base,
                    "declared": master.version,
                    "required": required_version,
                },
                stream_layer=layer,
            )
        )

    if not master.independent_segments:
        findings.append(
            R.MST_NO_INDEPENDENT_SEGMENTS.raise_finding(
                f"{master.final_url} carries no EXT-X-INDEPENDENT-SEGMENTS tag.",
                evidence=evidence_base,
                stream_layer=layer,
            )
        )

    if not master.iframe_variants:
        findings.append(
            R.MST_NO_IFRAME.raise_finding(
                f"{master.final_url} declares no EXT-X-I-FRAME-STREAM-INF entry.",
                evidence=evidence_base,
                stream_layer=layer,
            )
        )

    if master.session_keys:
        methods = sorted({key.get("METHOD", "NONE") for key in master.session_keys})
        findings.append(
            R.MST_DRM.raise_finding(
                f"{master.final_url} declares EXT-X-SESSION-KEY with METHOD {methods}. "
                "Bitstream checks run only on segments a supplied key decrypts.",
                evidence={**evidence_base, "methods": methods},
                stream_layer=layer,
            )
        )

    findings += _check_variants(master, layer=layer)
    findings += _check_ladder(master, layer=layer, thresholds=thresholds)
    findings += _check_rendition_groups(master, layer=layer)

    if not any(f.severity.rank >= 2 for f in findings):
        findings.append(
            R.MST_OK.raise_finding(
                f"{master.final_url} lists {len(master.variants)} rung(s), each carrying "
                "BANDWIDTH, RESOLUTION, FRAME-RATE and CODECS, with every referenced rendition "
                "group defined.",
                evidence={**evidence_base, "variant_count": len(master.variants)},
                stream_layer=layer,
            )
        )
    return findings


def _required_version(raw: str) -> int:
    required = 1
    for tag, version in TAG_MIN_VERSION.items():
        if tag in raw:
            required = max(required, version)
    return required


def _check_variants(master: MasterPlaylist, *, layer: StreamLayer) -> list[Finding]:
    findings: list[Finding] = []
    for variant in master.variants:
        evidence = {
            "variant": variant.variant_id,
            "uri": variant.resolved_uri,
            "line": variant.line.number,
            "raw_line": variant.line.text.strip(),
            "attrs": variant.attrs,
        }
        if variant.bandwidth is None:
            findings.append(
                R.MST_NO_BANDWIDTH.raise_finding(
                    f"Line {variant.line.number} declares no BANDWIDTH: "
                    f"{variant.line.text.strip()}",
                    evidence=evidence,
                    stream_layer=layer,
                    variant=variant.variant_id,
                )
            )
        if variant.resolution is None:
            findings.append(
                R.MST_NO_RESOLUTION.raise_finding(
                    f"Line {variant.line.number} declares no RESOLUTION: "
                    f"{variant.line.text.strip()}",
                    evidence=evidence,
                    stream_layer=layer,
                    variant=variant.variant_id,
                )
            )
        if variant.frame_rate is None:
            findings.append(
                R.MST_NO_FRAMERATE.raise_finding(
                    f"Line {variant.line.number} declares no FRAME-RATE: "
                    f"{variant.line.text.strip()}",
                    evidence=evidence,
                    stream_layer=layer,
                    variant=variant.variant_id,
                )
            )
        elif round(variant.frame_rate, 2) not in [round(f, 2) for f in VALIDATED_FRAME_RATES]:
            findings.append(
                R.MST_FRAMERATE_UNSUPPORTED.raise_finding(
                    f"{variant.variant_id} declares FRAME-RATE={variant.frame_rate}.",
                    evidence=evidence,
                    stream_layer=layer,
                    variant=variant.variant_id,
                )
            )
        if variant.codecs is None:
            findings.append(
                R.MST_NO_CODECS.raise_finding(
                    f"Line {variant.line.number} declares no CODECS: {variant.line.text.strip()}",
                    evidence=evidence,
                    stream_layer=layer,
                    variant=variant.variant_id,
                )
            )
        elif any(codec.strip().startswith(("hvc1", "hev1")) for codec in variant.codecs.split(",")):
            findings.append(
                R.MST_HEVC.raise_finding(
                    f'{variant.variant_id} declares CODECS="{variant.codecs}".',
                    evidence=evidence,
                    stream_layer=layer,
                    variant=variant.variant_id,
                )
            )
    return findings


def _check_ladder(
    master: MasterPlaylist, *, layer: StreamLayer, thresholds: Thresholds
) -> list[Finding]:
    findings: list[Finding] = []
    variants = [v for v in master.variants if v.bandwidth is not None]
    if not variants:
        return findings

    by_bandwidth = sorted(variants, key=lambda v: v.bandwidth or 0)
    first = master.variants[0]
    lowest = by_bandwidth[0]

    # Tizen starts on the first listed EXT-X-STREAM-INF, not on the lowest bitrate rung.
    if (
        first.bandwidth is not None
        and lowest.bandwidth is not None
        and first.bandwidth > lowest.bandwidth
    ):
        findings.append(
            R.MST_FIRST_RUNG_HIGH.raise_finding(
                f"The first listed rung is {first.variant_id} at "
                f"{(first.bandwidth or 0) // 1000} kbit/s while the lowest rung is "
                f"{lowest.variant_id} at {(lowest.bandwidth or 0) // 1000} kbit/s.",
                evidence={
                    "first_variant": first.variant_id,
                    "first_bandwidth": first.bandwidth,
                    "lowest_variant": lowest.variant_id,
                    "lowest_bandwidth": lowest.bandwidth,
                    "line": first.line.number,
                },
                stream_layer=layer,
                variant=first.variant_id,
            )
        )

    lowest_kbps = (lowest.bandwidth or 0) / 1000
    if lowest_kbps > thresholds.lowest_rung_max_kbps:
        findings.append(
            R.MST_LOWEST_RUNG_HIGH.raise_finding(
                f"The lowest rung {lowest.variant_id} is {lowest_kbps:.0f} kbit/s against a "
                f"recovery floor of {thresholds.lowest_rung_max_kbps} kbit/s.",
                evidence={"lowest_variant": lowest.variant_id, "lowest_kbps": lowest_kbps},
                stream_layer=layer,
                variant=lowest.variant_id,
            )
        )

    for lower, upper in itertools.pairwise(by_bandwidth):
        low_bw = lower.bandwidth or 1
        high_bw = upper.bandwidth or 1
        ratio = high_bw / low_bw
        if ratio > thresholds.max_adjacent_rung_ratio:
            findings.append(
                R.MST_RUNG_STEP.raise_finding(
                    f"{lower.variant_id} at {low_bw // 1000} kbit/s and {upper.variant_id} at "
                    f"{high_bw // 1000} kbit/s are a factor of {ratio:.2f} apart against a "
                    f"maximum of {thresholds.max_adjacent_rung_ratio}.",
                    evidence={"lower": lower.variant_id, "upper": upper.variant_id, "ratio": ratio},
                    stream_layer=layer,
                    variant=upper.variant_id,
                )
            )

    seen: dict[tuple[int | None, str | None], Variant] = {}
    for variant in variants:
        key = (variant.bandwidth, variant.resolution)
        if key in seen:
            findings.append(
                R.MST_DUPLICATE_RUNG.raise_finding(
                    f"{variant.variant_id} on line {variant.line.number} repeats "
                    f"BANDWIDTH={variant.bandwidth} RESOLUTION={variant.resolution} already "
                    f"declared on line {seen[key].line.number}.",
                    evidence={
                        "variant": variant.variant_id,
                        "line": variant.line.number,
                        "duplicate_of_line": seen[key].line.number,
                    },
                    stream_layer=layer,
                    variant=variant.variant_id,
                )
            )
        seen[key] = variant

    muxed = [v for v in master.variants if not v.audio_group]
    demuxed = [v for v in master.variants if v.audio_group]
    if muxed and demuxed:
        findings.append(
            R.MST_MIXED_MUX.raise_finding(
                f"{len(muxed)} rung(s) carry no AUDIO group and {len(demuxed)} rung(s) reference "
                f"one: muxed {[v.variant_id for v in muxed]}, demuxed "
                f"{[v.variant_id for v in demuxed]}.",
                evidence={
                    "muxed": [v.variant_id for v in muxed],
                    "demuxed": [v.variant_id for v in demuxed],
                },
                stream_layer=layer,
            )
        )
    return findings


def _check_rendition_groups(master: MasterPlaylist, *, layer: StreamLayer) -> list[Finding]:
    findings: list[Finding] = []
    audio_groups = {r.group_id for r in master.renditions if r.type == "AUDIO"}
    subtitle_groups = {r.group_id for r in master.renditions if r.type == "SUBTITLES"}

    for variant in master.variants:
        if variant.audio_group and variant.audio_group not in audio_groups:
            findings.append(
                R.MST_AUDIO_GROUP_MISSING.raise_finding(
                    f"{variant.variant_id} on line {variant.line.number} references "
                    f'AUDIO="{variant.audio_group}" and the playlist defines '
                    f"{sorted(audio_groups) or 'no audio group'}.",
                    evidence={
                        "variant": variant.variant_id,
                        "referenced": variant.audio_group,
                        "defined": sorted(audio_groups),
                        "line": variant.line.number,
                    },
                    stream_layer=layer,
                    variant=variant.variant_id,
                )
            )
        if variant.subtitles_group and variant.subtitles_group not in subtitle_groups:
            findings.append(
                R.MST_AUDIO_GROUP_MISSING.raise_finding(
                    f'{variant.variant_id} references SUBTITLES="{variant.subtitles_group}" and '
                    f"the playlist defines {sorted(subtitle_groups) or 'no subtitle group'}.",
                    evidence={
                        "variant": variant.variant_id,
                        "referenced": variant.subtitles_group,
                        "defined": sorted(subtitle_groups),
                    },
                    stream_layer=layer,
                    variant=variant.variant_id,
                )
            )

    for group in audio_groups:
        members = master.audio_renditions(group)
        channel_sets = {r.channels for r in members if r.channels}
        if len(channel_sets) > 1:
            findings.append(
                R.MST_AUDIO_GROUP_INCONSISTENT.raise_finding(
                    f'Audio group "{group}" declares CHANNELS values {sorted(channel_sets)} '
                    f"across {len(members)} rendition(s).",
                    evidence={"group": group, "channels": sorted(channel_sets)},
                    stream_layer=layer,
                )
            )
    return findings


def check_master_changed(
    previous: MasterPlaylist, current: MasterPlaylist, *, layer: StreamLayer
) -> list[Finding]:
    """Master re-poll comparison (§B, every 20 s)."""
    findings: list[Finding] = []
    before = {v.resolved_uri: v.attrs for v in previous.variants}
    after = {v.resolved_uri: v.attrs for v in current.variants}

    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = [uri for uri in set(before) & set(after) if before[uri] != after[uri]]

    if added or removed or changed:
        parts = []
        if added:
            parts.append(f"{len(added)} variant(s) added")
        if removed:
            parts.append(f"{len(removed)} variant(s) removed")
        if changed:
            parts.append(f"{len(changed)} variant(s) had attributes rewritten")
        findings.append(
            R.MST_CHANGED.raise_finding(
                f"{current.final_url} changed during the session: {', '.join(parts)}.",
                evidence={"added": added, "removed": removed, "changed": changed},
                stream_layer=layer,
            )
        )
    return findings


def check_cross_level(
    *,
    variant: Variant,
    measured: dict[str, Any],
    layer: StreamLayer,
) -> list[Finding]:
    """Manifest attributes against the values measured in the container and bitstream."""
    findings: list[Finding] = []
    sps = measured.get("sps") or {}
    aac = measured.get("aac_config") or {}
    evidence = {
        "variant": variant.variant_id,
        "declared": variant.attrs,
        "measured": {"sps": sps, "aac": aac, "audio_codec": measured.get("audio_codec")},
    }

    if variant.resolution and sps.get("resolution") and variant.resolution != sps["resolution"]:
        findings.append(
            R.MST_CROSS_LEVEL.raise_finding(
                f"{variant.variant_id} declares RESOLUTION={variant.resolution} and the sequence "
                f"parameter set codes {sps['resolution']}.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant.variant_id,
            )
        )

    declared_codecs = [c.strip() for c in (variant.codecs or "").split(",") if c.strip()]
    video_codec = next(
        (c for c in declared_codecs if c.startswith(("avc", "hvc", "hev", "dvh"))), None
    )
    audio_codec = next(
        (c for c in declared_codecs if c.startswith(("mp4a", "ac-3", "ec-3", "ac-4"))), None
    )

    if video_codec and sps.get("codec_string") and not _codec_family_matches(video_codec, sps):
        findings.append(
            R.MST_CODECS_MISMATCH.raise_finding(
                f'{variant.variant_id} declares CODECS="{video_codec}" and the bitstream carries '
                f"{sps.get('profile')} level {sps.get('level')} ({sps.get('codec_string')}).",
                evidence=evidence,
                stream_layer=layer,
                variant=variant.variant_id,
            )
        )

    declared_aac = audio_codec.lower() if audio_codec and audio_codec.startswith("mp4a") else None
    measured_aac = (aac.get("codec_string") or "").lower() or None
    if declared_aac and measured_aac and declared_aac != measured_aac:
        findings.append(
            R.AUD_CODEC_MISMATCH.raise_finding(
                f'{variant.variant_id} declares CODECS="{audio_codec}" and the elementary '
                f"stream carries {aac['codec_string']} ({aac.get('aot_name')}).",
                evidence=evidence,
                stream_layer=layer,
                variant=variant.variant_id,
            )
        )

    measured_audio = (measured.get("audio_codec") or "").lower()
    declared_dolby = (
        audio_codec.lower()
        if audio_codec and audio_codec.lower() in ("ec-3", "ac-3", "ac-4")
        else None
    )
    if declared_dolby and measured_audio and declared_dolby != measured_audio:
        findings.append(
            R.MST_DOLBY_MISMATCH.raise_finding(
                f'{variant.variant_id} declares CODECS="{audio_codec}" and the elementary '
                f"stream carries {measured_audio}.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant.variant_id,
            )
        )

    declared_fps = variant.frame_rate
    measured_fps = sps.get("frame_rate")
    if declared_fps and measured_fps and abs(declared_fps - measured_fps) > 0.5:
        findings.append(
            R.VID_FRAMERATE_MISMATCH.raise_finding(
                f"{variant.variant_id} declares FRAME-RATE={declared_fps} and the sequence "
                f"parameter set VUI timing gives {measured_fps:.3f}.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant.variant_id,
            )
        )

    video_range = variant.video_range.upper()
    transfer = sps.get("transfer_characteristics") or measured.get("transfer_characteristics")
    if video_range in ("PQ", "HLG") and transfer in (1, "1", None):
        findings.append(
            R.MST_VIDEO_RANGE_MISMATCH.raise_finding(
                f"{variant.variant_id} declares VIDEO-RANGE={video_range} and the bitstream "
                f"codes transfer characteristics {transfer!r}.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant.variant_id,
            )
        )
    return findings


def _codec_family_matches(declared: str, sps: dict[str, Any]) -> bool:
    family = declared.split(".")[0].lower()
    measured_codec = (sps.get("codec") or "").lower()
    if family in ("avc1", "avc3"):
        return measured_codec == "h264"
    if family in ("hvc1", "hev1", "dvh1", "dvhe"):
        return measured_codec == "hevc"
    return True


def check_channels_attribute(
    *,
    group_id: str,
    declared_channels: str | None,
    measured_channels: int | None,
    layer: StreamLayer,
    variant: str,
) -> Finding | None:
    if not declared_channels or measured_channels is None:
        return None
    declared_count = declared_channels.split("/")[0]
    if not declared_count.isdigit():
        return None
    if int(declared_count) == measured_channels:
        return None
    return R.MST_CHANNELS_MISMATCH.raise_finding(
        f'Audio group "{group_id}" declares CHANNELS="{declared_channels}" and the elementary '
        f"stream carries {measured_channels} channel(s).",
        evidence={
            "group": group_id,
            "declared": declared_channels,
            "measured": measured_channels,
        },
        stream_layer=layer,
        variant=variant,
    )


def check_variant_reachable(
    *, variant: Variant, status: int, layer: StreamLayer, error: str | None = None
) -> Finding | None:
    if 200 <= status < 300:
        return None
    return R.MST_RUNG_UNREACHABLE.raise_finding(
        f"{variant.variant_id} is listed in the master playlist and its media playlist "
        f"{variant.resolved_uri} returned "
        + (f"HTTP {status}." if status else f"no response: {error}."),
        evidence={
            "variant": variant.variant_id,
            "uri": variant.resolved_uri,
            "status": status,
            "error": error,
        },
        stream_layer=layer,
        variant=variant.variant_id,
    )
