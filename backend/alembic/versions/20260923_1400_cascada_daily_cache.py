"""store the CASCADA historical (per-day) rebuffering window per channel

The historical source reads one value per UTC day for the last seven complete days. It is
cached in a table of its own so a realtime window and a historical one for the same channel
never overwrite each other; `cascada_samples` stays the per-minute record the batch pipeline
reads.

`create_all` at startup creates this table on a database that lacks it, before anyone runs a
migration. The upgrade therefore creates it only when it is absent, so running it afterwards
records the revision instead of failing with "table already exists".

Revision ID: a4d8e61b0c73
Revises: f1c7a2d93e58
Create Date: 2026-09-23 14:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.config import get_settings

revision = "a4d8e61b0c73"
down_revision = "f1c7a2d93e58"
branch_labels = None
depends_on = None

DAILY = get_settings().table_name("cascada_daily_samples")


def _already_created() -> bool:
    """True when startup's `create_all` made the table before this revision ran.

    `alembic upgrade --sql` writes the DDL without a database to look at, so it is always
    written in full there.
    """
    if op.get_context().as_sql:
        return False
    return DAILY in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _already_created():
        return
    op.create_table(
        DAILY,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("service_id", sa.String(length=64), nullable=False),
        sa.Column("channel_name", sa.String(length=255), nullable=False),
        sa.Column("country", sa.String(length=8), nullable=True),
        # The first and last day's 00:00 UTC, both days included.
        sa.Column("window_from", sa.BigInteger(), nullable=False),
        sa.Column("window_to", sa.BigInteger(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("average_pct", sa.Float(), nullable=True),
        sa.Column("days_with_data", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("series", sa.JSON(), nullable=True),
    )
    op.create_index(f"ix_{DAILY}_service_id", DAILY, ["service_id"])
    op.create_index(f"ix_{DAILY}_country", DAILY, ["country"])
    op.create_index(f"ix_{DAILY}_fetched_at", DAILY, ["fetched_at"])
    op.create_index(
        "ix_cascada_daily_window",
        DAILY,
        ["service_id", "window_from", "window_to"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_cascada_daily_window", table_name=DAILY)
    op.drop_index(f"ix_{DAILY}_fetched_at", table_name=DAILY)
    op.drop_index(f"ix_{DAILY}_country", table_name=DAILY)
    op.drop_index(f"ix_{DAILY}_service_id", table_name=DAILY)
    op.drop_table(DAILY)
