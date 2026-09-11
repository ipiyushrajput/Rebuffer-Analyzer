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
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import get_settings

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
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
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
    bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    )


class Report(Base):
    __tablename__ = _t("reports")

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    channel_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_type: Mapped[str] = mapped_column(String(16), default="realtime")
    format: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(Text)
    verdict_status: Mapped[str | None] = mapped_column(String(48), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(32), nullable=True)
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )


class SettingRow(Base):
    __tablename__ = _t("settings")

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
