"""The analyzer's account of its own timing.

Every freshness, cadence and stall measurement assumes the analyzer polled when it meant to.
A host short of CPU, or a detector that held the event loop, delays the poll instead — and the
delay reads as the stream's: a playlist that did not advance, a segment that arrived late, a
buffer that ran dry. This module measures the two ways the analyzer itself can be late:

* **Event-loop lag** — the session's one-second supervisor wakes later than one second. Any
  stall of the loop delays every poller and every segment fetch with it.
* **Handler time** — the analyzer spent longer handling one playlist poll than the poll
  interval it was due to keep, so the next poll of that rendition went out late by its own
  doing.

When either crosses its threshold the session raises `INFO-007` naming the measured delay, so a
reader of the report sees that the window was measured on a host that fell behind and which
timings are affected. A run that kept time states that too, in the result's `self_check`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class _HandlerStats:
    count: int = 0
    total_s: float = 0.0
    max_s: float = 0.0
    over_budget: int = 0
    budget_s: float = 0.0


@dataclass
class SelfCheck:
    """Counts what the analyzer's own timing did over one run."""

    lag_threshold_s: float
    ticks: int = 0
    lag_max_s: float = 0.0
    lag_total_s: float = 0.0
    late_ticks: int = 0
    handlers: dict[str, _HandlerStats] = field(default_factory=dict)

    def tick(self, expected_s: float, actual_s: float) -> None:
        """One supervisor wake: how long it waited against how long it asked to."""
        lag = max(0.0, actual_s - expected_s)
        self.ticks += 1
        self.lag_total_s += lag
        self.lag_max_s = max(self.lag_max_s, lag)
        if lag >= self.lag_threshold_s:
            self.late_ticks += 1

    def handled(self, variant: str, seconds: float, budget_s: float) -> None:
        """One playlist poll handled in `seconds`, against the poll interval it had to keep."""
        stats = self.handlers.setdefault(variant, _HandlerStats())
        stats.count += 1
        stats.total_s += seconds
        stats.max_s = max(stats.max_s, seconds)
        stats.budget_s = budget_s
        if seconds > budget_s:
            stats.over_budget += 1

    @property
    def late_handlers(self) -> dict[str, _HandlerStats]:
        return {name: stats for name, stats in self.handlers.items() if stats.over_budget}

    @property
    def kept_time(self) -> bool:
        return self.late_ticks == 0 and not self.late_handlers

    def statement(self) -> str:
        """One definite sentence on what the analyzer's timing was."""
        if self.ticks == 0 and not self.handlers:
            return "The session ended before the analyzer's own timing was measured."
        if self.kept_time:
            return (
                f"The analyzer kept time: the event loop ran at most "
                f"{self.lag_max_s * 1000:.0f} ms late across {self.ticks} supervisor wake(s), "
                f"and every playlist poll was handled inside its poll interval."
            )
        parts: list[str] = []
        if self.late_ticks:
            parts.append(
                f"the event loop ran {self.lag_max_s * 1000:.0f} ms late at worst and at least "
                f"{self.lag_threshold_s * 1000:.0f} ms late on {self.late_ticks} of "
                f"{self.ticks} supervisor wake(s)"
            )
        late = self.late_handlers
        if late:
            worst_name, worst = max(late.items(), key=lambda item: item[1].max_s)
            total = sum(stats.over_budget for stats in late.values())
            parts.append(
                f"{total} playlist poll(s) on {len(late)} rendition(s) took longer to handle "
                f"than their poll interval, the longest {worst.max_s * 1000:.0f} ms on "
                f"{worst_name} against an interval of {worst.budget_s * 1000:.0f} ms"
            )
        return (
            "The analyzer fell behind its own schedule: "
            + "; ".join(parts)
            + ". Poll timestamps, playlist freshness and segment arrival times in this window "
            "carry that delay."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kept_time": self.kept_time,
            "statement": self.statement(),
            "lag_threshold_ms": round(self.lag_threshold_s * 1000, 1),
            "ticks": self.ticks,
            "late_ticks": self.late_ticks,
            "lag_max_ms": round(self.lag_max_s * 1000, 1),
            "lag_mean_ms": round(self.lag_total_s / self.ticks * 1000, 1) if self.ticks else 0.0,
            "handlers": {
                name: {
                    "polls": stats.count,
                    "max_ms": round(stats.max_s * 1000, 1),
                    "mean_ms": round(stats.total_s / stats.count * 1000, 1) if stats.count else 0.0,
                    "budget_ms": round(stats.budget_s * 1000, 1),
                    "over_budget": stats.over_budget,
                }
                for name, stats in sorted(self.handlers.items())
            },
        }
