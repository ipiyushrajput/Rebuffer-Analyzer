"""What an automated batch is configured with, and how a running batch keeps its values.

Two rules shape this module.

**One source of truth.** The rebuffering threshold and the CASCADA window already exist as
`Thresholds.cascada_rebuffering_threshold_pct` and `Thresholds.cascada_window_days`, editable
in the Settings tab and used by the CASCADA Data tab. A batch reads those, rather than
carrying a second copy that could disagree with the tab an operator was just looking at.
What lives here is what is new: how long each channel is analysed for, how many run at once,
how long a batch may take, and what happens to the channels afterwards.

**A running batch keeps what it started with.** `BatchSettings.snapshot()` freezes the
effective values — the batch's own plus the two thresholds it read — into the batch row. An
edit made while a batch runs changes the next batch, never the one in flight, and the report
states the values that produced it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field

from app.config import Thresholds, get_thresholds
from app.db import session as db_session
from app.db.models import SettingRow

SETTINGS_KEY = "automated_batch"


class BatchSettings(BaseModel):
    """The batch block. Every field is editable in the Settings tab and stored in the DB."""

    # How long each selected channel is analysed for. Two minutes is a snapshot: long enough
    # to poll the ladder several times and sample segments, short enough that a country of a
    # hundred channels finishes inside the runtime cap.
    analysis_duration_minutes: int = Field(default=2, ge=1, le=240)
    # How many channels are analysed at once. Each one polls a ladder and downloads segments,
    # so this is the real load the host and the CDNs see.
    analysis_concurrency: int = Field(default=4, ge=1, le=16)
    # A batch that would run past this stops selecting new channels and says how many it did
    # not reach, rather than running for an unbounded time.
    max_runtime_minutes: int = Field(default=240, ge=10, le=1440)

    # Continuous aging after a **scheduled** batch. A manual batch never starts aging.
    aging_enabled: bool = Field(default=True)
    aging_duration_days: int = Field(default=7, ge=1, le=28)
    # Each aging run polls its ladder for days on end; this is the ceiling on how many of
    # those a host carries at once. Channels past it are named as skipped, worst first.
    aging_max_concurrent: int = Field(default=5, ge=1, le=32)

    # Correlating a rebuffering spike with what aging captured. The clocks are different
    # machines, so an event this far either side of a spike window still counts as inside it.
    correlation_tolerance_s: int = Field(default=120, ge=0, le=3600)
    # A spike window is this many consecutive minutes above the threshold. One minute is a
    # spike; requiring more ignores a single reading.
    spike_min_minutes: int = Field(default=1, ge=1, le=120)

    # How many batches to keep per country. Zero keeps everything, which is the default:
    # nothing is ever deleted unless an operator asks for it.
    retention_per_country: int = Field(default=0, ge=0, le=1000)

    def analysis_duration_s(self) -> float:
        return self.analysis_duration_minutes * 60.0

    def estimated_runtime_minutes(self, channels: int) -> float:
        """How long analysing this many channels takes at the configured parallelism."""
        if channels <= 0:
            return 0.0
        waves = -(-channels // max(1, self.analysis_concurrency))
        return waves * self.analysis_duration_minutes

    def channels_within_runtime(self) -> int:
        """How many channels fit inside the runtime cap."""
        per_wave = max(1, self.analysis_concurrency)
        waves = max(1, self.max_runtime_minutes // max(1, self.analysis_duration_minutes))
        return int(waves * per_wave)

    def snapshot(self, thresholds: Thresholds | None = None) -> dict[str, Any]:
        """The effective configuration, frozen into the batch that is starting.

        The two threshold values are copied in deliberately: the report has to state the
        window and the threshold its averages were judged against, and those can be edited
        between the run and the reading.
        """
        limits = thresholds or get_thresholds()
        return {
            **self.model_dump(),
            "threshold_pct": limits.cascada_rebuffering_threshold_pct,
            "window_days": limits.cascada_window_days,
            "scan_concurrency": limits.cascada_scan_concurrency,
            "captured_at": dt.datetime.now(dt.UTC).isoformat(),
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any] | None) -> BatchSettings:
        """Read a batch's frozen settings back, ignoring the threshold copies."""
        fields = {
            name: value for name, value in (snapshot or {}).items() if name in cls.model_fields
        }
        return cls(**fields)


DEFAULTS = BatchSettings()


async def load() -> BatchSettings:
    """The stored batch settings, or the defaults when none have been saved."""
    try:
        async with db_session.session_scope() as session:
            row = await session.get(SettingRow, SETTINGS_KEY)
            if row is not None and isinstance(row.value, dict):
                return BatchSettings(**{**DEFAULTS.model_dump(), **row.value})
    except Exception:
        # A settings table that does not answer must not stop a batch from running with the
        # documented defaults; the caller logs the reason through its own path.
        return BatchSettings()
    return BatchSettings()


async def save(settings: BatchSettings) -> BatchSettings:
    """Store the batch settings. A batch already running keeps the snapshot it started with."""
    async with db_session.session_scope() as session:
        row = await session.get(SettingRow, SETTINGS_KEY)
        if row is None:
            session.add(SettingRow(key=SETTINGS_KEY, value=settings.model_dump()))
        else:
            row.value = settings.model_dump()
    return settings
