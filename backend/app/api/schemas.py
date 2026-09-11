"""Request and response models shared by the REST routers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.analysis.engine import OPTIONAL_CHECK_SETS, SessionOptions
from app.config import USER_AGENT_PROFILES

AGING_PRESETS_MINUTES = (15, 30, 60, 180, 360, 720, 1440)


class JobOptionsIn(BaseModel):
    """Job options (§1.3). Clear keys are held in memory and never persisted or printed."""

    ua_profile: str = "tizen5"
    check_sets: list[str] = Field(default_factory=list)
    renditions: list[str] | None = None
    record_evidence: bool | None = None
    clear_keys: dict[str, str] = Field(default_factory=dict)
    vpb_mode: Literal["STRICT", "NORMAL", "OUTAGE_ONLY"] | None = None
    nth_segment_sampling: int | None = Field(default=None, ge=1, le=50)

    @field_validator("ua_profile")
    @classmethod
    def _known_profile(cls, value: str) -> str:
        if value not in USER_AGENT_PROFILES:
            raise ValueError(f"Unknown User-Agent profile: {value}")
        return value

    @field_validator("check_sets")
    @classmethod
    def _known_check_sets(cls, value: list[str]) -> list[str]:
        unknown = [name for name in value if name not in OPTIONAL_CHECK_SETS]
        if unknown:
            raise ValueError(f"Unknown check set(s): {unknown}")
        return value

    def to_session_options(self, *, duration_s: float, default_record: bool) -> SessionOptions:
        return SessionOptions(
            duration_s=duration_s,
            ua_profile=self.ua_profile,
            check_sets=("baseline", *dict.fromkeys(self.check_sets)),
            renditions=tuple(self.renditions) if self.renditions else None,
            record_evidence=self.record_evidence
            if self.record_evidence is not None
            else default_record,
            clear_keys=dict(self.clear_keys),
            vpb_mode=self.vpb_mode,
            nth_segment_sampling=self.nth_segment_sampling,
            snapshot_mode=duration_s <= 300,
        )


class UrlSet(BaseModel):
    playback_url: str = Field(min_length=8)
    origin_url: str | None = None
    cdn_url: str | None = None
    ssai_url: str | None = None
    channel_name: str | None = None

    @field_validator("playback_url", "origin_url", "cdn_url", "ssai_url")
    @classmethod
    def _absolute(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("URLs must be absolute and start with http:// or https://")
        return value


class RealtimeSessionIn(UrlSet):
    options: JobOptionsIn = Field(default_factory=JobOptionsIn)
    # Realtime runs until stopped; this is the upper bound the session manager enforces.
    max_duration_minutes: int = Field(default=120, ge=1, le=1440)


class AgingJobIn(UrlSet):
    duration_minutes: int = Field(default=60, ge=1, le=1440)
    options: JobOptionsIn = Field(default_factory=JobOptionsIn)


class BulkJobIn(BaseModel):
    mode: Literal["snapshot", "aging"] = "snapshot"
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    concurrency: int = Field(default=5, ge=1, le=50)
    options: JobOptionsIn = Field(default_factory=JobOptionsIn)


class JobSummaryOut(BaseModel):
    id: str
    type: str
    status: str
    channel_name: str
    progress: float
    elapsed_s: float
    remaining_s: float
    counts: dict[str, int] = Field(default_factory=dict)
    verdict: dict[str, Any] | None = None
    error: str | None = None
