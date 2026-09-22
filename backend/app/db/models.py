"""Relational schema (§2.4.7).

Table names run through `Settings.table_name`, so the whole schema can live inside an
existing database with an `rba_` prefix when the DB user cannot create a database of its
own. Nothing here touches another application's tables.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import get_settings

# A rendered report is about a megabyte. MySQL's default BLOB caps at 64 KB, so the column
# is declared LONGBLOB there; SQLite and PostgreSQL take the generic type unchanged.
ReportBlob = LargeBinary().with_variant(mysql.LONGBLOB(), "mysql", "mariadb")

_prefix = get_settings().db_table_prefix


def _t(name: str) -> str:
    return f"{_prefix}{name}"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Channel(Base):
    __tablename__ = _t("channels")

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    channel_ref: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    content_provider: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cdn: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Job(Base):
    __tablename__ = _t("jobs")

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    type: Mapped[str] = mapped_column(String(16), index=True)  # realtime | aging | bulk
    status: Mapped[str] = mapped_column(String(16), index=True, default="PENDING")
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{_t('channels')}.id"), nullable=True
    )
    channel_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    parent_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    playback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    cdn_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    ssai_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Indexed: every listing of jobs is newest-first, and without it the server sorts the
    # whole matching set — JSON columns and all — to return the first page.
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )
    verdict: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    findings: Mapped[list[Finding]] = relationship(back_populates="job", cascade="all, delete")


class BulkItem(Base):
    __tablename__ = _t("bulk_items")

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bulk_job_id: Mapped[str] = mapped_column(String(36), index=True)
    row_index: Mapped[int] = mapped_column(Integer)
    channel_name: Mapped[str] = mapped_column(String(255))
    playback_url: Mapped[str] = mapped_column(Text)
    origin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    cdn_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    ssai_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    child_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    verdict_status: Mapped[str | None] = mapped_column(String(48), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Finding(Base):
    __tablename__ = _t("findings")

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey(f"{_t('jobs')}.id"), index=True)
    rule_id: Mapped[str] = mapped_column(String(16), index=True)
    severity: Mapped[str] = mapped_column(String(10), index=True)
    layer: Mapped[str] = mapped_column(String(24))
    stream_layer: Mapped[str] = mapped_column(String(16), default="PLAYBACK")
    owner: Mapped[str] = mapped_column(String(24), index=True)
    variant: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    detail: Mapped[str] = mapped_column(Text)
    root_cause: Mapped[str] = mapped_column(Text)
    fix: Mapped[str] = mapped_column(Text)
    rebuffer_impact: Mapped[str] = mapped_column(String(12), default="none")
    count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)
    layer_presence: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    job: Mapped[Job] = relationship(back_populates="findings")


class Incident(Base):
    __tablename__ = _t("incidents")

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey(f"{_t('jobs')}.id"), index=True)
    variant: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    cause_finding_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    cause_chain: Mapped[list[Any]] = mapped_column(JSON, default=list)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PlaylistSample(Base):
    __tablename__ = _t("samples_playlist")

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    stream_layer: Mapped[str] = mapped_column(String(16), default="PLAYBACK")
    variant: Mapped[str] = mapped_column(String(64))
    msn: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0)
    last_msn: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0)
    dsn: Mapped[int] = mapped_column(Integer, default=0)
    seg_count: Mapped[int] = mapped_column(Integer, default=0)
    target_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    window_s: Mapped[float] = mapped_column(Float, default=0.0)
    freshness_s: Mapped[float] = mapped_column(Float, default=0.0)
    http_status: Mapped[int] = mapped_column(Integer, default=0)
    ttfb_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_ms: Mapped[float] = mapped_column(Float, default=0.0)
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(16), default="UNKNOWN")

    __table_args__ = (
        Index(f"ix_{_t('samples_playlist')}_job_variant_ts", "job_id", "variant", "ts"),
        # Every rung at once, in time order: the charts and the exports read a job
        # whole. Without this the server sorts the whole run — URIs, JSON and all.
        Index(f"ix_{_t('samples_playlist')}_job_ts", "job_id", "ts"),
    )


class SegmentSample(Base):
    __tablename__ = _t("samples_segment")

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    stream_layer: Mapped[str] = mapped_column(String(16), default="PLAYBACK")
    variant: Mapped[str] = mapped_column(String(64))
    msn: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0)
    uri_hash: Mapped[str] = mapped_column(String(64))
    uri: Mapped[str] = mapped_column(Text)
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration_declared: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    download_ms: Mapped[float] = mapped_column(Float, default=0.0)
    ttfb_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    http_status: Mapped[int] = mapped_column(Integer, default=0)
    measured_kbps: Mapped[float | None] = mapped_column(Float, nullable=True)
    pts_start: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), nullable=True
    )
    pts_end: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), nullable=True
    )
    av_skew_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (
        Index(f"ix_{_t('samples_segment')}_job_variant_ts", "job_id", "variant", "ts"),
        # Every rung at once, in time order: the charts and the exports read a job
        # whole. Without this the server sorts the whole run — URIs, JSON and all.
        Index(f"ix_{_t('samples_segment')}_job_ts", "job_id", "ts"),
    )


class PlayerSample(Base):
    __tablename__ = _t("samples_player")

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    event: Mapped[str] = mapped_column(String(32))
    buffer_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The rendition's own bitrate, from the rung the player switched to. A ladder tops out in
    # the low tens of megabits, but this is BIGINT anyway: the column used to be a signed INT
    # and a browser reporting anything past 2^31 took the whole batch of samples down with it.
    bitrate: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), nullable=True
    )
    # What the player measured the network doing, which is a different quantity from the
    # bitrate above: hls.js's estimate on a small segment off a nearby CDN reads in gigabits
    # per second. Keeping the two apart is what stops the played-rung chart plotting
    # throughput spikes as if they were rung changes.
    bandwidth_bps: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), nullable=True
    )
    dropped_frames: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stall_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class VirtualBufferSample(Base):
    __tablename__ = _t("virtual_buffer")

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    variant: Mapped[str] = mapped_column(String(64))
    level_s: Mapped[float] = mapped_column(Float, default=0.0)
    state: Mapped[str] = mapped_column(String(16), default="PLAYING")

    __table_args__ = (
        Index(f"ix_{_t('virtual_buffer')}_job_variant_ts", "job_id", "variant", "ts"),
        # Every rung at once, in time order: the charts and the exports read a job
        # whole. Without this the server sorts the whole run — URIs, JSON and all.
        Index(f"ix_{_t('virtual_buffer')}_job_ts", "job_id", "ts"),
    )


class PlaylistSnapshot(Base):
    __tablename__ = _t("playlist_snapshots")

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    stream_layer: Mapped[str] = mapped_column(String(16), default="PLAYBACK")
    variant: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(Text)
    raw: Mapped[str] = mapped_column(Text)
    msn: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index(f"ix_{_t('playlist_snapshots')}_job_variant_ts", "job_id", "variant", "ts"),
        # Every rung at once, in time order: the charts and the exports read a job
        # whole. Without this the server sorts the whole run — URIs, JSON and all.
        Index(f"ix_{_t('playlist_snapshots')}_job_ts", "job_id", "ts"),
    )


class Report(Base):
    __tablename__ = _t("reports")

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    channel_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_type: Mapped[str] = mapped_column(String(16), default="realtime")
    format: Mapped[str] = mapped_column(String(8))
    # The report itself. Stored here so a deployment keeps nothing on local disk and a
    # report survives the container it was rendered in. Deferred: a listing reads a couple
    # of hundred rows and must not drag a megabyte of HTML along with each one.
    content: Mapped[bytes | None] = mapped_column(ReportBlob, nullable=True, deferred=True)
    # Set only when a copy was also written to disk, which is off by default.
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict_status: Mapped[str | None] = mapped_column(String(48), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(32), nullable=True)
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )


class Batch(Base):
    """One automated run over a country: scan, select, analyse, report.

    A batch is backend work, not a screen. The browser that started it can close, reload or
    move on, and a restart mid-run resumes from the first incomplete phase, because every
    phase writes its progress here and its channels to `batch_items`.
    """

    __tablename__ = _t("batches")

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    country: Mapped[str] = mapped_column(String(8), index=True)
    # `manual` when an operator pressed Start Batch, `scheduled` when the weekly firing did.
    # Only a scheduled batch starts continuous aging.
    kind: Mapped[str] = mapped_column(String(16), default="manual", index=True)
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True)
    phase: Mapped[str] = mapped_column(String(24), default="QUEUED")

    channels_listed: Mapped[int] = mapped_column(Integer, default=0)
    channels_scanned: Mapped[int] = mapped_column(Integer, default=0)
    channels_above: Mapped[int] = mapped_column(Integer, default=0)
    channels_analysed: Mapped[int] = mapped_column(Integer, default=0)
    channels_failed: Mapped[int] = mapped_column(Integer, default=0)

    # The CASCADA window the averages cover, as whole epoch seconds. The report labels its
    # column from this rather than from whatever the setting says today.
    window_from: Mapped[int] = mapped_column(BigInteger, default=0)
    window_to: Mapped[int] = mapped_column(BigInteger, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The settings as they were when this batch started. A Settings edit mid-run must not
    # change what a running batch does, and the report states the values it actually used.
    settings_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    items: Mapped[list[BatchItem]] = relationship(back_populates="batch", cascade="all, delete")


class BatchItem(Base):
    """One channel inside a batch: what it measured, what was run, and what came back."""

    __tablename__ = _t("batch_items")

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey(f"{_t('batches')}.id"), index=True)
    service_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_name: Mapped[str] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    playback_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The CASCADA average that selected this channel, as a percentage of viewing time.
    average_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    minutes_above: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    # The analysis this channel produced, so the report links into the per-channel view.
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # The aging run started for this channel after a scheduled batch, when there was one.
    aging_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Spike windows matched against aging events, computed when the next report is generated.
    correlation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    batch: Mapped[Batch] = relationship(back_populates="items")


class BatchLog(Base):
    """Every step a batch took, so a run can be read back after the fact."""

    __tablename__ = _t("batch_logs")

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    level: Mapped[str] = mapped_column(String(8), default="INFO")
    message: Mapped[str] = mapped_column(Text)


class BatchSchedule(Base):
    """When a country's batch runs by itself.

    The schedule is a row rather than an in-process timer, so it survives a restart and can be
    edited in Settings. `last_fired_at` records every firing including a skip; `last_success_at`
    is what the seven-day gap is measured from.
    """

    __tablename__ = _t("batch_schedules")

    country: Mapped[str] = mapped_column(String(8), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Monday is 0, matching `datetime.weekday()`.
    weekday: Mapped[int] = mapped_column(Integer, default=0)
    hour_utc: Mapped[int] = mapped_column(Integer, default=2)
    minute_utc: Mapped[int] = mapped_column(Integer, default=0)
    # Overrides of the global batch settings for this country; empty means use the global ones.
    overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_fired_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class CascadaSample(Base):
    """One channel's CASCADA rebuffering window, kept so a scan is paid for once.

    A country scan is one CASCADA call per channel and takes minutes; storing the result here
    rather than in the process means a finished scan survives a restart, a reopened modal does
    not re-hit CASCADA, and a second analyzer instance serves the same country report. A row
    older than `cascada_cache_ttl_minutes` is refetched.
    """

    __tablename__ = _t("cascada_samples")

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_name: Mapped[str] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    # The window the figures below describe, as whole epoch seconds, which is what keys a row.
    window_from: Mapped[int] = mapped_column(BigInteger)
    window_to: Mapped[int] = mapped_column(BigInteger)
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )
    # Percentages, as CASCADA reports them: 0.159 is 0.159% of viewing time.
    average_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    minutes_above: Mapped[int] = mapped_column(Integer, default=0)
    minutes_counted: Mapped[int] = mapped_column(Integer, default=0)
    previous_week_average_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    above_threshold: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # True when fewer minutes came back than the window asked for, so a reader knows the
    # average covers less than its label says.
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    # Both series, so the modal and the per-channel report are served without another call.
    series: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (
        Index("ix_cascada_window", "service_id", "window_from", "window_to", unique=True),
    )


class SettingRow(Base):
    __tablename__ = _t("settings")

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
