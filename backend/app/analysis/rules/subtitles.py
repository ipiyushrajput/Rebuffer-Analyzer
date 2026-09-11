"""Subtitle and caption detectors."""

from __future__ import annotations

import itertools
import re
from typing import Any

from app.analysis.rules import catalogue as R
from app.analysis.rules.base import Finding, StreamLayer

CUE_TIMING = re.compile(r"(\d{2}:)?\d{2}:\d{2}\.\d{3}\s*-->\s*(\d{2}:)?\d{2}:\d{2}\.\d{3}")
# C0 controls other than tab, line feed and carriage return.
C0_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _to_seconds(stamp: str) -> float:
    parts = stamp.strip().split(":")
    seconds = float(parts[-1])
    if len(parts) >= 2:
        seconds += int(parts[-2]) * 60
    if len(parts) == 3:
        seconds += int(parts[-3]) * 3600
    return seconds


def check_webvtt(text: str, *, variant: str, uri: str, layer: StreamLayer) -> list[Finding]:
    findings: list[Finding] = []
    evidence: dict[str, Any] = {"variant": variant, "uri": uri}

    body = text.lstrip("﻿")
    if not body.startswith("WEBVTT"):
        findings.append(
            R.SUB_PARSE.raise_finding(
                f"{uri} starts with {body[:16]!r} rather than the WEBVTT signature.",
                evidence={**evidence, "head": body[:64]},
                stream_layer=layer,
                variant=variant,
            )
        )
        return findings

    if "X-TIMESTAMP-MAP" not in body:
        findings.append(
            R.SUB_NO_TIMESTAMP_MAP.raise_finding(
                f"{uri} carries no X-TIMESTAMP-MAP header.",
                evidence={**evidence, "head": body[:128]},
                stream_layer=layer,
                variant=variant,
            )
        )

    control_chars = C0_CONTROLS.findall(body)
    if control_chars:
        findings.append(
            R.SUB_CONTROL_CHARS.raise_finding(
                f"{uri} contains {len(control_chars)} C0 control character(s): "
                f"{sorted({hex(ord(c)) for c in control_chars})}.",
                evidence={**evidence, "count": len(control_chars)},
                stream_layer=layer,
                variant=variant,
            )
        )

    cues: list[tuple[float, float]] = []
    for line in body.splitlines():
        if "-->" not in line:
            continue
        if not CUE_TIMING.search(line):
            findings.append(
                R.SUB_PARSE.raise_finding(
                    f"{uri} carries a cue timing line that does not parse: {line.strip()!r}",
                    evidence={**evidence, "line": line.strip()},
                    stream_layer=layer,
                    variant=variant,
                )
            )
            continue
        start_text, _, end_text = line.partition("-->")
        end_text = end_text.strip().split(" ")[0]
        cues.append((_to_seconds(start_text), _to_seconds(end_text)))

    for (start_a, end_a), (start_b, _end_b) in itertools.pairwise(cues):
        if start_b < end_a and start_b >= start_a:
            findings.append(
                R.SUB_CUE_OVERLAP.raise_finding(
                    f"{uri} has a cue starting at {start_b:.3f} s while the previous cue runs to "
                    f"{end_a:.3f} s.",
                    evidence={**evidence, "overlap_s": end_a - start_b},
                    stream_layer=layer,
                    variant=variant,
                )
            )
            break

    if not findings:
        findings.append(
            R.SUB_OK.raise_finding(
                f"{uri} parses as WebVTT with {len(cues)} cue(s) and carries a timestamp map.",
                evidence={**evidence, "cue_count": len(cues)},
                stream_layer=layer,
                variant=variant,
            )
        )
    return findings


def check_caption_consistency(
    captions_by_variant: dict[str, bool],
    declared_by_variant: dict[str, str | None],
    *,
    layer: StreamLayer,
) -> list[Finding]:
    """In-band CEA captions must be present on every rung that declares them."""
    findings: list[Finding] = []
    for variant, declared in declared_by_variant.items():
        carried = captions_by_variant.get(variant)
        if carried is None:
            continue
        declares = bool(declared) and declared.upper() != "NONE"
        if declares and not carried:
            findings.append(
                R.SUB_CEA_ABSENT.raise_finding(
                    f'{variant} declares CLOSED-CAPTIONS="{declared}" and its sampled segments '
                    "carry no CEA-608 or CEA-708 data.",
                    evidence={"variant": variant, "declared": declared, "carried": carried},
                    stream_layer=layer,
                    variant=variant,
                )
            )
        elif not declares and carried:
            findings.append(
                R.SUB_CC_ATTR_MISMATCH.raise_finding(
                    f"{variant} declares CLOSED-CAPTIONS={declared or 'nothing'} and its sampled "
                    "segments carry in-band caption data.",
                    evidence={"variant": variant, "declared": declared, "carried": carried},
                    stream_layer=layer,
                    variant=variant,
                )
            )

    carrying = {v for v, carried in captions_by_variant.items() if carried}
    if carrying and len(carrying) != len(captions_by_variant):
        absent = sorted(set(captions_by_variant) - carrying)
        findings.append(
            R.SUB_CEA_ABSENT.raise_finding(
                f"In-band captions are carried on {sorted(carrying)} and absent on {absent}, so "
                "captions disappear when ABR selects a rung in the second group.",
                evidence={"carrying": sorted(carrying), "absent": absent},
                stream_layer=layer,
            )
        )
    return findings
