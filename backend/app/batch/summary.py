"""The `Issues` and `Analysis Summary` cells: what the run found, for the content partner.

The batch report is sent to whoever packages and delivers the channel, so both cells carry
only what that reader can act on:

- only ERROR and CRITICAL findings — WARN, INFO and PASS describe the run, not a defect to fix;
- no rule identifiers (`SEG-010`, `MED-004`), which are this tool's own vocabulary;
- none of the rules named in `report_excluded_rules`, which describe how the live window
  behaved while it was watched rather than what was packaged;
- nothing about rebuffering spikes, aging or correlation, which are how this team chose the
  channel, not what is wrong with it. The correlation sheet keeps that for the team.

`Issues` is one line per distinct issue, worst first: `Issue 1 - <title>`. A rule that fired on
several renditions is one issue, not one per rendition. `Analysis Summary` takes the same
numbering and gives each issue the sentence the analysis report leads with — the rule's root
cause followed by the measured evidence of its worst occurrence — so a reader can match the
two columns line by line.

Neither cell explains beyond what the rule recorded, and neither is ever blank: a channel with
nothing to report says so, because an empty cell reads as an oversight.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# What the partner report carries, worst first.
REPORTED_SEVERITIES = ("CRITICAL", "ERROR")

NO_ISSUES = "No error or critical issue was found."
NOTHING_FOUND = "The analysis completed and found no error or critical issue in the stream."
NOT_ANALYSED = "This channel was not analysed."
NO_URL = "The catalogue lists no playback URL for this channel, so it could not be analysed."

# A parenthesis that names a rule goes whole — `(see SEG-010)` — and then any bare identifier
# left in the text: `SEG-010`, `INFO-005`.
_RULE_ASIDE = re.compile(r"\s*\([^()]*\b[A-Z]{2,5}-\d{3}\b[^()]*\)")
_RULE_ID = re.compile(r"\s*\b[A-Z]{2,5}-\d{3}\b")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Issue:
    """One distinct defect, however many renditions it fired on."""

    title: str
    explanation: str
    variants: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChannelSummary:
    """A channel's two report cells."""

    issues: str
    analysis: str


def _clean(text: str | None) -> str:
    """Rule text without this tool's identifiers, on one line."""
    stripped = _RULE_ID.sub("", _RULE_ASIDE.sub("", text or ""))
    return _SPACES.sub(" ", stripped).strip()


def _sentence(text: str) -> str:
    return text if not text or text.endswith((".", "!", "?")) else f"{text}."


def _severity(finding: dict[str, Any]) -> str:
    return str(finding.get("severity", "")).upper()


def _rank(severity: str) -> int:
    return REPORTED_SEVERITIES.index(severity) if severity in REPORTED_SEVERITIES else 99


def issues(findings: Iterable[dict[str, Any]], excluded_rules: Iterable[str] = ()) -> list[Issue]:
    """The ERROR and CRITICAL findings as distinct issues, worst first."""
    excluded = {rule.strip().upper() for rule in excluded_rules}
    groups: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        rule_id = str(finding.get("rule_id") or "").upper()
        if _severity(finding) not in REPORTED_SEVERITIES or rule_id in excluded:
            continue
        groups.setdefault(rule_id or str(finding.get("title", "")), []).append(finding)

    def worst(group: list[dict[str, Any]]) -> dict[str, Any]:
        # The occurrence to quote: the most severe, then the one seen most, then the earliest.
        return min(
            group,
            key=lambda f: (
                _rank(_severity(f)),
                -int(f.get("count") or 1),
                str(f.get("first_seen") or ""),
            ),
        )

    ordered = sorted(
        groups.values(),
        key=lambda group: (
            _rank(_severity(worst(group))),
            min(str(f.get("first_seen") or "") for f in group),
        ),
    )
    result: list[Issue] = []
    for group in ordered:
        quoted = worst(group)
        explanation = " ".join(
            part
            for part in (
                _sentence(_clean(quoted.get("root_cause"))),
                _sentence(_clean(quoted.get("detail"))),
            )
            if part
        )
        variants = tuple(sorted({str(f["variant"]) for f in group if f.get("variant")}))
        result.append(
            Issue(
                title=_clean(quoted.get("title")) or "Unnamed issue",
                explanation=explanation,
                variants=variants,
            )
        )
    return result


def _issues_cell(found: list[Issue]) -> str:
    return "\n".join(f"Issue {n} - {issue.title}" for n, issue in enumerate(found, 1))


def _analysis_cell(found: list[Issue]) -> str:
    paragraphs = []
    for n, issue in enumerate(found, 1):
        text = issue.explanation or issue.title
        if len(issue.variants) > 1:
            text += f" Seen on {len(issue.variants)} renditions: {', '.join(issue.variants)}."
        paragraphs.append(f"Issue {n} - {text}")
    return "\n\n".join(paragraphs)


def build(
    *,
    status: str,
    findings: list[dict[str, Any]] | None = None,
    excluded_rules: Iterable[str] = (),
    error: str | None = None,
) -> ChannelSummary:
    """One channel's Issues and Analysis Summary cells."""
    if status == "NO_URL":
        return ChannelSummary(issues="", analysis=NO_URL)
    if status in ("INSUFFICIENT_DATA", "NO_PROVIDER"):
        # Judged by nobody: the reason is the whole statement.
        return ChannelSummary(issues="", analysis=error or NOT_ANALYSED)
    if status == "FAILED":
        return ChannelSummary(
            issues="", analysis=f"The analysis failed: {error or 'no reason was recorded'}."
        )
    if status == "SKIPPED":
        return ChannelSummary(
            issues="",
            analysis=(
                "This channel was selected but not reached: "
                f"{error or 'the batch stopped before it ran'}."
            ),
        )
    if status not in ("ANALYSED", "COMPLETED"):
        return ChannelSummary(issues="", analysis=NOT_ANALYSED)

    found = issues(findings or [], excluded_rules)
    if not found:
        return ChannelSummary(issues=NO_ISSUES, analysis=NOTHING_FOUND)
    return ChannelSummary(issues=_issues_cell(found), analysis=_analysis_cell(found))
