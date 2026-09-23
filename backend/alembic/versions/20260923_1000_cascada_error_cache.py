"""store the CASCADA error window per channel

The error view reads the same CASCADA endpoint as the rebuffering view with
`target_metrics[]=error_count`, and its answer is cached for the same reason: a reopened modal
and a downloaded report should not pay for a second call. It gets a table of its own rather
than a metric column on `cascada_samples`, whose figures are percentages and which the batch
pipeline reads as the rebuffering record.

`create_all` at startup creates this table on a database that lacks it, before anyone runs a
migration. The upgrade therefore creates it only when it is absent, so running it afterwards
records the revision instead of failing with "table already exists".

Revision ID: f1c7a2d93e58
Revises: e9b3f207c6d4
Create Date: 2026-09-23 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.config import get_settings

revision = "f1c7a2d93e58"
down_revision = "e9b3f207c6d4"
branch_labels = None
depends_on = None

ERRORS = get_settings().table_name("cascada_error_samples")


def _already_created() -> bool:
    """True when startup's `create_all` made the table before this revision ran.

    `alembic upgrade --sql` writes the DDL without a database to look at, so it is always
    written in full there.
    """
    if op.get_context().as_sql:
        return False
    return ERRORS in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _already_created():
        return
    op.create_table(
        ERRORS,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("service_id", sa.String(length=64), nullable=False),
        sa.Column("channel_name", sa.String(length=255), nullable=False),
        sa.Column("country", sa.String(length=8), nullable=True),
        sa.Column("window_from", sa.BigInteger(), nullable=False),
        sa.Column("window_to", sa.BigInteger(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        # Errors per minute, as CASCADA reports them, and their sum over the window.
        sa.Column("average_per_min", sa.Float(), nullable=True),
        sa.Column("max_per_min", sa.Float(), nullable=True),
        sa.Column("max_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total", sa.Float(), nullable=True),
        sa.Column("previous_week_average_per_min", sa.Float(), nullable=True),
        sa.Column("minutes_counted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("series", sa.JSON(), nullable=True),
    )
    op.create_index(f"ix_{ERRORS}_service_id", ERRORS, ["service_id"])
    op.create_index(f"ix_{ERRORS}_country", ERRORS, ["country"])
    op.create_index(f"ix_{ERRORS}_fetched_at", ERRORS, ["fetched_at"])
    # One row per channel per window, so a refetch replaces rather than accumulates.
    op.create_index(
        "ix_cascada_error_window",
        ERRORS,
        ["service_id", "window_from", "window_to"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_cascada_error_window", table_name=ERRORS)
    op.drop_index(f"ix_{ERRORS}_fetched_at", table_name=ERRORS)
    op.drop_index(f"ix_{ERRORS}_country", table_name=ERRORS)
    op.drop_index(f"ix_{ERRORS}_service_id", table_name=ERRORS)
    op.drop_table(ERRORS)
