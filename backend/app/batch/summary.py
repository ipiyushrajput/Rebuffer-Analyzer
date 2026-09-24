"""The `Analysis Summary` cell: what the run found, in plain statements.

The column has one job — tell a reader what was observed for this channel without making them
open the analysis. So it states the findings by severity, the incidents by count, and, when a
continuous aging run was correlated against the rebuffering timeline, what was captured
inside each spike window.

What it never does is explain. The findings already carry a root cause, written by the rule
that fired; this cell reports that a rule fired, not a theory about why. A channel with
nothing to report says so, because an empty cell reads as an oversight.
"""

from __future__ import annotations

from typing import Any

# Worst first, so the sentence opens with what matters.
SEVERITY_ORDER = ("CRITICAL", "ERROR", "WARN", "INFO")

NOTHING_FOUND = "The analysis completed and recorded no finding."
NOT_ANALYSED = "This channel was not analysed."
NO_URL = "The catalogue lists no playback URL for this channel, so it could not be analysed."


def _counted(findings: list[dict[str, Any]]) -> str:
    """Findings as counts by severity: `2 ERROR, 1 WARN`."""
    tally: dict[str, int] = {}
    for finding in findings:
        severity = str(finding.get("severity", "INFO")).upper()
        tally[severity] = tally.get(severity, 0) + 1
    ordered = [f"{tally[name]} {name}" for name in SEVERITY_ORDER if tally.get(name)]
    extra = sorted(name for name in tally if name not in SEVERITY_ORDER)
    ordered += [f"{tally[name]} {name}" for name in extra]
    return ", ".join(ordered)


def _headlines(findings: list[dict[str, Any]], limit: int = 4) -> list[str]:
    """The worst few findings, each as the rule said it."""
    ranked = sorted(
        findings,
        key=lambda f: (
            SEVERITY_ORDER.index(str(f.get("severity", "INFO")).upper())
            if str(f.get("severity", "INFO")).upper() in SEVERITY_ORDER
            else len(SEVERITY_ORDER)
        ),
    )
    return [f"{item.get('rule_id', '')} {item.get('title', '')}".strip() for item in ranked[:limit]]


def build(
    *,
    status: str,
    findings: list[dict[str, Any]] | None = None,
    incidents: list[dict[str, Any]] | None = None,
    correlation: dict[str, Any] | None = None,
    error: str | None = None,
) -> str:
    """One channel's summary cell."""
    if status == "NO_URL":
        return NO_URL
    if status in ("INSUFFICIENT_DATA", "NO_PROVIDER"):
        # Judged by nobody: the reason is the whole statement.
        return error or NOT_ANALYSED
    if status == "FAILED":
        return f"The analysis failed: {error or 'no reason was recorded'}."
    if status == "SKIPPED":
        return (
            "This channel was selected but not reached: "
            f"{error or 'the batch stopped before it ran'}."
        )
    if status not in ("ANALYSED", "COMPLETED"):
        return NOT_ANALYSED

    found = findings or []
    opened = incidents or []
    parts: list[str] = []

    if found:
        parts.append(
            f"{len(found)} finding(s) — {_counted(found)}: " + "; ".join(_headlines(found)) + "."
        )
    else:
        parts.append(NOTHING_FOUND)

    if opened:
        parts.append(f"{len(opened)} incident(s) opened during the run.")

    coverage = (correlation or {}).get("coverage") or ""
    if coverage.startswith("Not correlated"):
        parts.append(coverage)
    if (correlation or {}).get("error"):
        parts.append(str((correlation or {})["error"]))

    windows = (correlation or {}).get("windows") or []
    if windows:
        matched = (correlation or {}).get("matched_count", 0)
        parts.append(
            f"{len(windows)} rebuffering spike window(s) in the measured week, "
            f"{matched} with an aging event captured in them."
        )
        # The windows themselves carry the detail; the first few make the cell useful without
        # making it unreadable. The correlated sheet holds every one.
        parts.extend(window["sentence"] for window in windows[:3])
        if len(windows) > 3:
            parts.append(
                f"{len(windows) - 3} further spike window(s) are listed in the correlation sheet."
            )

    return " ".join(parts)
