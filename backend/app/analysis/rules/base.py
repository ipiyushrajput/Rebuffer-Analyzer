"""Rule model and registry.

A rule is a declaration, not a function: it carries the identifier, the layer it applies to,
the severity, the responsible owner, the single definite sentence that states the root
cause, and the single instruction that fixes it. Detector code measures, then raises the
declared rule with the evidence that proves it.

Wording is fixed at declaration time so `tests/test_forbidden_words.py` can scan the whole
catalogue and fail the build if a hedging word ever enters it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    ERROR = "ERROR"
    WARN = "WARN"
    INFO = "INFO"
    PASS = "PASS"

    @property
    def rank(self) -> int:
        return {"CRITICAL": 4, "ERROR": 3, "WARN": 2, "INFO": 1, "PASS": 0}[self.value]


class Owner(str, Enum):
    CONTENT_PROVIDER = "CONTENT_PROVIDER"
    PACKAGER = "PACKAGER"
    CDN = "CDN"
    SSAI = "SSAI"
    SAMSUNG_PLAYER = "SAMSUNG_PLAYER"
    NETWORK = "NETWORK"

    @property
    def label(self) -> str:
        return {
            "CONTENT_PROVIDER": "Content provider",
            "PACKAGER": "Packager",
            "CDN": "CDN",
            "SSAI": "SSAI vendor",
            "SAMSUNG_PLAYER": "Samsung player / device team",
            "NETWORK": "Network",
        }[self.value]


class StreamLayer(str, Enum):
    """Which of the four supplied URLs a finding was measured on."""

    PLAYBACK = "PLAYBACK"
    ORIGIN = "ORIGIN"
    CDN = "CDN"
    SSAI = "SSAI"


class RebufferImpact(str, Enum):
    DIRECT = "direct"
    INDIRECT = "indirect"
    NONE = "none"

    @property
    def weight(self) -> float:
        return {"direct": 1.0, "indirect": 0.5, "none": 0.1}[self.value]


# Severities an operator has reassigned, by rule id. A team that has accepted a defect, or
# that reads one more seriously than the catalogue declares, changes what the rule reports
# without editing the catalogue: the declaration stays the product's opinion, this is the
# deployment's. It is loaded from the database at startup and edited in Settings.
_severity_overrides: dict[str, Severity] = {}


def set_severity_overrides(overrides: dict[str, Severity]) -> dict[str, Severity]:
    """Replace the whole override map. Returns what is now in force."""
    _severity_overrides.clear()
    _severity_overrides.update(overrides)
    return dict(_severity_overrides)


def severity_overrides() -> dict[str, Severity]:
    return dict(_severity_overrides)


def effective_severity(rule_id: str, declared: Severity) -> Severity:
    return _severity_overrides.get(rule_id, declared)


@dataclass(frozen=True, slots=True)
class Rule:
    """One declared check."""

    id: str
    layer: str
    severity: Severity
    owner: Owner
    title: str
    root_cause: str
    fix: str
    rebuffer_impact: RebufferImpact = RebufferImpact.NONE
    reference: str = ""
    thresholds: tuple[str, ...] = ()

    def raise_finding(
        self,
        detail: str,
        *,
        evidence: dict[str, Any] | None = None,
        variant: str | None = None,
        stream_layer: StreamLayer = StreamLayer.PLAYBACK,
        severity: Severity | None = None,
        owner: Owner | None = None,
        at: dt.datetime | None = None,
    ) -> Finding:
        now = at or dt.datetime.now(dt.UTC)
        # An operator's override is about the rule, so it outranks both the declaration and a
        # per-finding severity a detector computed: "treat this one as a warning" has to mean
        # every finding the rule raises, or it does not mean anything.
        chosen = _severity_overrides.get(self.id) or severity or self.severity
        return Finding(
            rule=self,
            detail=detail,
            variant=variant,
            stream_layer=stream_layer,
            severity=chosen,
            owner=owner or self.owner,
            first_seen=now,
            last_seen=now,
            evidence=[evidence] if evidence else [],
        )


@dataclass(slots=True)
class Finding:
    """A rule that fired, with the measurements that prove it."""

    rule: Rule
    detail: str
    variant: str | None = None
    stream_layer: StreamLayer = StreamLayer.PLAYBACK
    severity: Severity = Severity.ERROR
    owner: Owner = Owner.CDN
    count: int = 1
    first_seen: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    last_seen: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    evidence: list[dict[str, Any]] = field(default_factory=list)
    layer_presence: dict[str, bool] = field(default_factory=dict)

    MAX_EVIDENCE = 5

    @property
    def key(self) -> tuple[str, str, str]:
        """De-duplication key: rule, stream layer, variant (§3.5)."""
        return (self.rule.id, self.stream_layer.value, self.variant or "")

    def merge(self, other: Finding) -> None:
        self.count += other.count
        self.last_seen = max(self.last_seen, other.last_seen)
        self.first_seen = min(self.first_seen, other.first_seen)
        for sample in other.evidence:
            if len(self.evidence) < self.MAX_EVIDENCE:
                self.evidence.append(sample)
        if other.severity.rank > self.severity.rank:
            self.severity = other.severity
        self.layer_presence.update(other.layer_presence)
        if other.detail and other.detail != self.detail:
            self.detail = other.detail

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule.id,
            "title": self.rule.title,
            "layer": self.rule.layer,
            "stream_layer": self.stream_layer.value,
            "severity": self.severity.value,
            "owner": self.owner.value,
            "owner_label": self.owner.label,
            "variant": self.variant,
            "detail": self.detail,
            "root_cause": self.rule.root_cause,
            "fix": self.rule.fix,
            "rebuffer_impact": self.rule.rebuffer_impact.value,
            "count": self.count,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "evidence": self.evidence,
            "layer_presence": self.layer_presence,
            "reference": self.rule.reference,
        }


class RuleRegistry:
    """Every declared rule, keyed by id."""

    def __init__(self) -> None:
        self._rules: dict[str, Rule] = {}

    def register(self, rule: Rule) -> Rule:
        if rule.id in self._rules:
            raise ValueError(f"Rule {rule.id} is declared twice")
        self._rules[rule.id] = rule
        return rule

    def declare(self, **kwargs: Any) -> Rule:
        return self.register(Rule(**kwargs))

    def get(self, rule_id: str) -> Rule:
        return self._rules[rule_id]

    def all(self) -> list[Rule]:
        return sorted(self._rules.values(), key=lambda r: r.id)

    def by_prefix(self, prefix: str) -> list[Rule]:
        return [r for r in self.all() if r.id.startswith(prefix)]

    def __len__(self) -> int:
        return len(self._rules)

    def __contains__(self, rule_id: object) -> bool:
        return rule_id in self._rules


registry = RuleRegistry()


class FindingCollector:
    """Aggregates findings by `(rule_id, stream_layer, variant)` as they are emitted."""

    def __init__(self) -> None:
        self._findings: dict[tuple[str, str, str], Finding] = {}

    def add(self, finding: Finding | None) -> Finding | None:
        if finding is None:
            return None
        existing = self._findings.get(finding.key)
        if existing is None:
            self._findings[finding.key] = finding
            return finding
        existing.merge(finding)
        return existing

    def extend(self, findings: Iterable[Finding | None]) -> None:
        for finding in findings:
            self.add(finding)

    def all(self) -> list[Finding]:
        return sorted(
            self._findings.values(),
            key=lambda f: (-f.severity.rank, -f.count, f.rule.id),
        )

    def worst_severity(self) -> Severity:
        actionable = [f for f in self._findings.values() if f.severity not in (Severity.PASS,)]
        if not actionable:
            return Severity.PASS
        return max((f.severity for f in actionable), key=lambda s: s.rank)

    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in Severity}
        for finding in self._findings.values():
            out[finding.severity.value] += 1
        return out

    def by_id(self, rule_id: str) -> list[Finding]:
        return [f for f in self._findings.values() if f.rule.id == rule_id]

    def has(self, rule_id: str) -> bool:
        return any(f.rule.id == rule_id for f in self._findings.values())

    def __len__(self) -> int:
        return len(self._findings)
