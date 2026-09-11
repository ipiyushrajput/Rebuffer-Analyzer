"""Owner attribution.

With comparison URLs, a defect is pinned to the first layer it appears on:
ORIGIN → the content provider or packager, CDN-only → the CDN, SSAI-only → the SSAI vendor.

With only the playback URL, the owner comes from the rule's own layer plus the response
headers that prove it, and the evidence line says exactly what proved it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.analysis.rules.base import Finding, Owner, StreamLayer

LAYER_ORDER = (StreamLayer.ORIGIN, StreamLayer.CDN, StreamLayer.SSAI, StreamLayer.PLAYBACK)

# When a defect first appears at a layer, that layer's operator owns it.
LAYER_OWNER = {
    StreamLayer.ORIGIN: Owner.PACKAGER,
    StreamLayer.CDN: Owner.CDN,
    StreamLayer.SSAI: Owner.SSAI,
}

# Rules whose owner is decided by the defect itself, not by the layer it was seen on.
LAYER_INDEPENDENT_PREFIXES = ("VID-", "AUD-", "AV-", "MST-", "SUB-")


@dataclass(slots=True)
class LayerDiff:
    rule_id: str
    variant: str | None
    present_on: list[str]
    first_layer: str | None
    statement: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "variant": self.variant,
            "present_on": self.present_on,
            "first_layer": self.first_layer,
            "statement": self.statement,
        }


def attribute(
    findings: list[Finding], *, layers_analysed: set[StreamLayer]
) -> tuple[list[Finding], list[LayerDiff]]:
    """Assign the owner of every finding and produce the layer diff."""
    comparison = len(layers_analysed - {StreamLayer.PLAYBACK}) > 0
    by_rule: dict[tuple[str, str | None], list[Finding]] = {}
    for finding in findings:
        by_rule.setdefault((finding.rule.id, finding.variant), []).append(finding)

    diffs: list[LayerDiff] = []
    for (rule_id, variant), group in by_rule.items():
        present = [f.stream_layer for f in group]
        present_names = sorted({layer.value for layer in present})
        first = _first_layer(present)

        for finding in group:
            finding.layer_presence = {layer.value: layer in present for layer in layers_analysed}

        if comparison and first is not None:
            statement = _statement(first, present_names)
            owner = LAYER_OWNER.get(first)
            if owner and not rule_id.startswith(LAYER_INDEPENDENT_PREFIXES):
                for finding in group:
                    finding.owner = owner
            diffs.append(
                LayerDiff(
                    rule_id=rule_id,
                    variant=variant,
                    present_on=present_names,
                    first_layer=first.value,
                    statement=statement,
                )
            )
        else:
            for finding in group:
                _attribute_from_headers(finding)

    return findings, diffs


def _first_layer(present: list[StreamLayer]) -> StreamLayer | None:
    for layer in LAYER_ORDER:
        if layer in present:
            return layer
    return None


def _statement(first: StreamLayer, present_names: list[str]) -> str:
    if first is StreamLayer.ORIGIN:
        return f"Present at ORIGIN. Also measured on {present_names}."
    if first is StreamLayer.CDN:
        return f"First appears at CDN; the origin is clean. Measured on {present_names}."
    if first is StreamLayer.SSAI:
        return f"Introduced by SSAI; the origin and the CDN are clean. Measured on {present_names}."
    return f"Measured on the playback URL only ({present_names})."


def _attribute_from_headers(finding: Finding) -> None:
    """Prove the owner from the response headers when no comparison URL was supplied."""
    headers = _headers_of(finding)
    if not headers:
        return

    age = headers.get("age")
    x_cache = (headers.get("x-cache") or "").upper()
    proof: str | None = None

    if age and age.strip().isdigit() and int(age) > 0 and "HIT" in x_cache:
        proof = (
            f"The response carried Age: {age} with X-Cache: {headers.get('x-cache')}, which "
            "proves the edge served this from cache rather than the origin."
        )
        finding.owner = Owner.CDN
    elif "MISS" in x_cache:
        proof = (
            f"The response carried X-Cache: {headers.get('x-cache')}, which proves the edge "
            "fetched this from the origin."
        )
        if not finding.rule.id.startswith(("CDN-", "HTTP-", "NET-", "TLS-")):
            finding.owner = Owner.PACKAGER

    pop = headers.get("x-amz-cf-pop") or headers.get("x-served-by")
    if pop:
        proof = (proof or "") + f" Edge: {pop}."

    if proof:
        finding.evidence.append({"attribution_proof": proof.strip(), "headers": headers})


def _headers_of(finding: Finding) -> dict[str, str]:
    for sample in finding.evidence:
        headers = sample.get("headers") or sample.get("cdn_headers")
        if isinstance(headers, dict) and headers:
            return {str(k).lower(): str(v) for k, v in headers.items()}
    return {}


def owner_summary(findings: list[Finding]) -> dict[str, int]:
    """How many actionable findings each owner holds."""
    from app.analysis.rules.base import Severity

    summary: dict[str, int] = {}
    for finding in findings:
        if finding.severity in (Severity.PASS, Severity.INFO):
            continue
        summary[finding.owner.value] = summary.get(finding.owner.value, 0) + 1
    return dict(sorted(summary.items(), key=lambda item: -item[1]))
