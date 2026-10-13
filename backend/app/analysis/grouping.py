"""One issue, however many renditions it was measured on.

The engine keeps a finding per rule, stream layer and rendition, which is right for the
evidence: each rendition has its own playlist, its own segments and its own poll. Read as a
list, that turned one defect on a four-rung ladder into four alarms. A group is the same rule on
the same layer: the worst severity, the summed occurrences, the widest time span, and every
rendition's own finding kept intact beneath it. The report, the escalation text and the
screens group the same way (`frontend/src/lib/findingGroups.ts` is the screen's copy).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from app.analysis.rules.base import Finding, Severity


@dataclass(slots=True)
class FindingGroup:
    rule_id: str
    stream_layer: str
    members: list[Finding] = field(default_factory=list)

    @property
    def lead(self) -> Finding:
        return self.members[0]

    @property
    def severity(self) -> Severity:
        return self.lead.severity

    @property
    def count(self) -> int:
        return sum(member.count for member in self.members)

    @property
    def first_seen(self) -> dt.datetime:
        return min(member.first_seen for member in self.members)

    @property
    def last_seen(self) -> dt.datetime:
        return max(member.last_seen for member in self.members)

    @property
    def renditions(self) -> list[str]:
        return [member.variant for member in self.members if member.variant]

    def rendition_label(self) -> str:
        names = self.renditions
        if not names:
            return "the whole ladder"
        if len(self.members) == 1:
            return names[0]
        return ", ".join(f"{m.variant or 'ladder'} ({m.count})" for m in self.members)


def _weight(finding: Finding) -> tuple[int, int, str]:
    return (-finding.severity.rank, -finding.count, finding.variant or "")


def group_findings(findings: list[Finding]) -> list[FindingGroup]:
    """Groups worst first: severity, then total occurrences, then rule id."""
    groups: dict[tuple[str, str], FindingGroup] = {}
    for finding in findings:
        key = (finding.rule.id, finding.stream_layer.value)
        group = groups.setdefault(key, FindingGroup(rule_id=key[0], stream_layer=key[1]))
        group.members.append(finding)
    for group in groups.values():
        group.members.sort(key=_weight)
    return sorted(groups.values(), key=lambda g: (-g.severity.rank, -g.count, g.rule_id))
