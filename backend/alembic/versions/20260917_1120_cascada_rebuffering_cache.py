"""store the CASCADA rebuffering window per channel

A country scan is one CASCADA call per channel and takes minutes to walk a whole country.
Keeping each channel's window here means a finished scan survives a restart, a reopened
modal costs nothing, and a second analyzer instance serves the same country report. The row
is a cache: `fetched_at` against `cascada_cache_ttl_minutes` decides whether it is served or
refetched, and dropping the table loses no analysis.

Revision ID: b7f2c4e81a36
Revises: 8c41a7d9b2e5
Create Date: 2026-09-17 11:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.config import get_settings

revision = "b7f2c4e81a36"
down_revision = "8c41a7d9b2e5"
branch_labels = None
depends_on = None

CASCADA_SAMPLES = get_settings().table_name("cascada_samples")


def upgrade() -> None:
    op.create_table(
        CASCADA_SAMPLES,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("service_id", sa.String(length=64), nullable=False),
        sa.Column("channel_name", sa.String(length=255), nullable=False),
        sa.Column("country", sa.String(length=8), nullable=True),
        sa.Column("window_from", sa.BigInteger(), nullable=False),
        sa.Column("window_to", sa.BigInteger(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        # Percentages, as CASCADA reports them: 0.159 is 0.159% of viewing time.
        sa.Column("average_pct", sa.Float(), nullable=True),
        sa.Column("max_pct", sa.Float(), nullable=True),
        sa.Column("max_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("minutes_above", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("minutes_counted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("previous_week_average_pct", sa.Float(), nullable=True),
        sa.Column("above_threshold", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("series", sa.JSON(), nullable=True),
    )
    op.create_index(f"ix_{CASCADA_SAMPLES}_service_id", CASCADA_SAMPLES, ["service_id"])
    op.create_index(f"ix_{CASCADA_SAMPLES}_country", CASCADA_SAMPLES, ["country"])
    op.create_index(f"ix_{CASCADA_SAMPLES}_fetched_at", CASCADA_SAMPLES, ["fetched_at"])
    op.create_index(f"ix_{CASCADA_SAMPLES}_above_threshold", CASCADA_SAMPLES, ["above_threshold"])
    # One row per channel per window, so a refetch replaces rather than accumulates.
    op.create_index(
        "ix_cascada_window",
        CASCADA_SAMPLES,
        ["service_id", "window_from", "window_to"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_cascada_window", table_name=CASCADA_SAMPLES)
    op.drop_index(f"ix_{CASCADA_SAMPLES}_above_threshold", table_name=CASCADA_SAMPLES)
    op.drop_index(f"ix_{CASCADA_SAMPLES}_fetched_at", table_name=CASCADA_SAMPLES)
    op.drop_index(f"ix_{CASCADA_SAMPLES}_country", table_name=CASCADA_SAMPLES)
    op.drop_index(f"ix_{CASCADA_SAMPLES}_service_id", table_name=CASCADA_SAMPLES)
    op.drop_table(CASCADA_SAMPLES)
