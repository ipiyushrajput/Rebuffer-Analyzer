"""store automated batches, their channels, their log and their schedule

An automated batch scans a country, selects the channels rebuffering above the threshold,
analyses each one and produces a report. It is backend work: the browser that started it can
close, and a restart mid-run resumes from the first incomplete phase. That is only possible
because every phase writes here — the batch's progress, each channel's state, the log, and
the weekly schedule, which is a row rather than an in-process timer.

Revision ID: c3a9d1f47b28
Revises: b7f2c4e81a36
Create Date: 2026-09-18 09:40:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.config import get_settings

revision = "c3a9d1f47b28"
down_revision = "b7f2c4e81a36"
branch_labels = None
depends_on = None

_settings = get_settings()
BATCHES = _settings.table_name("batches")
BATCH_ITEMS = _settings.table_name("batch_items")
BATCH_LOGS = _settings.table_name("batch_logs")
BATCH_SCHEDULES = _settings.table_name("batch_schedules")


def upgrade() -> None:
    op.create_table(
        BATCHES,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("country", sa.String(length=8), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="QUEUED"),
        sa.Column("phase", sa.String(length=24), nullable=False, server_default="QUEUED"),
        sa.Column("channels_listed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("channels_scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("channels_above", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("channels_analysed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("channels_failed", sa.Integer(), nullable=False, server_default="0"),
        # The CASCADA window the averages cover, as whole epoch seconds.
        sa.Column("window_from", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("window_to", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        # The settings as they were when the batch started, so a later edit cannot change
        # what a running batch does and the report states what it actually used.
        sa.Column("settings_snapshot", sa.JSON(), nullable=True),
    )
    op.create_index(f"ix_{BATCHES}_country", BATCHES, ["country"])
    op.create_index(f"ix_{BATCHES}_kind", BATCHES, ["kind"])
    op.create_index(f"ix_{BATCHES}_status", BATCHES, ["status"])
    op.create_index(f"ix_{BATCHES}_created_at", BATCHES, ["created_at"])

    op.create_table(
        BATCH_ITEMS,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.String(length=36), sa.ForeignKey(f"{BATCHES}.id"), nullable=False),
        sa.Column("service_id", sa.String(length=64), nullable=False),
        sa.Column("channel_name", sa.String(length=255), nullable=False),
        sa.Column("country", sa.String(length=8), nullable=True),
        sa.Column("playback_url", sa.Text(), nullable=True),
        sa.Column("average_pct", sa.Float(), nullable=True),
        sa.Column("max_pct", sa.Float(), nullable=True),
        sa.Column("minutes_above", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("aging_job_id", sa.String(length=36), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("correlation", sa.JSON(), nullable=True),
    )
    op.create_index(f"ix_{BATCH_ITEMS}_batch_id", BATCH_ITEMS, ["batch_id"])
    op.create_index(f"ix_{BATCH_ITEMS}_service_id", BATCH_ITEMS, ["service_id"])
    op.create_index(f"ix_{BATCH_ITEMS}_status", BATCH_ITEMS, ["status"])
    op.create_index(f"ix_{BATCH_ITEMS}_job_id", BATCH_ITEMS, ["job_id"])
    op.create_index(f"ix_{BATCH_ITEMS}_aging_job_id", BATCH_ITEMS, ["aging_job_id"])

    op.create_table(
        BATCH_LOGS,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=8), nullable=False, server_default="INFO"),
        sa.Column("message", sa.Text(), nullable=False),
    )
    op.create_index(f"ix_{BATCH_LOGS}_batch_id", BATCH_LOGS, ["batch_id"])
    op.create_index(f"ix_{BATCH_LOGS}_at", BATCH_LOGS, ["at"])

    op.create_table(
        BATCH_SCHEDULES,
        sa.Column("country", sa.String(length=8), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        # Monday is 0, matching `datetime.weekday()`.
        sa.Column("weekday", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hour_utc", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("minute_utc", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("overrides", sa.JSON(), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table(BATCH_SCHEDULES)
    op.drop_index(f"ix_{BATCH_LOGS}_at", table_name=BATCH_LOGS)
    op.drop_index(f"ix_{BATCH_LOGS}_batch_id", table_name=BATCH_LOGS)
    op.drop_table(BATCH_LOGS)
    for name in ("aging_job_id", "job_id", "status", "service_id", "batch_id"):
        op.drop_index(f"ix_{BATCH_ITEMS}_{name}", table_name=BATCH_ITEMS)
    op.drop_table(BATCH_ITEMS)
    for name in ("created_at", "status", "kind", "country"):
        op.drop_index(f"ix_{BATCHES}_{name}", table_name=BATCHES)
    op.drop_table(BATCHES)
