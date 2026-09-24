"""One question, two CASCADA sources: what was this channel's rebuffering?

`realtime` is the per-minute series for the rolling window that ends at the current minute,
read by `service.channel_window`, unchanged. `historical` is one value per UTC day for the
last seven complete days, read by `historical.channel_days`. Both answer with a
`ChannelWindow`, so whoever asks — a report, a batch, the modal — reads the same fields and
the switch between the two sources happens here and in `service.start_scan(source=)`, nowhere
else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.cascada import historical, service
from app.cascada.series import Point
from app.cascada.store import ChannelWindow

Source = Literal["realtime", "historical"]


@dataclass(frozen=True, slots=True)
class ChannelRebuffering:
    """A channel's measured rebuffering, whichever source measured it."""

    source: str
    granularity: str
    points: list[Point]
    average_pct: float | None
    window_start: str
    window_end: str
    window: ChannelWindow

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "granularity": self.granularity,
            "average_pct": self.average_pct,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "points": [point.as_dict() for point in self.points],
        }


def _dates(entry: ChannelWindow) -> tuple[str, str]:
    """The window as it is stated: returned days for historical, the rolling window otherwise."""
    if entry.source == historical.SOURCE:
        returned = entry.details.get("returned_days") or {}
        return str(returned.get("first_day", "")), str(returned.get("last_day", ""))
    return entry.window.start.isoformat(), entry.window.end.isoformat()


async def get_channel_rebuffering(
    *,
    service_id: str,
    channel_name: str,
    country: str,
    source: Source,
    refresh: bool = False,
) -> ChannelRebuffering:
    if source == historical.SOURCE:
        entry = await historical.channel_days(
            service_id=service_id, channel_name=channel_name, country=country, refresh=refresh
        )
    else:
        entry = await service.channel_window(
            service_id=service_id, channel_name=channel_name, country=country, refresh=refresh
        )
    start, end = _dates(entry)
    return ChannelRebuffering(
        source=entry.source,
        granularity=entry.granularity,
        points=entry.origin,
        average_pct=entry.stats.average_pct,
        window_start=start,
        window_end=end,
        window=entry,
    )
