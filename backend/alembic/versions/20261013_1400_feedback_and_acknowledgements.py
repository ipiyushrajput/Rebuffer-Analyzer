"""finding feedback and per-channel acknowledgements

`finding_feedback` holds an engineer's verdict on a finding (confirmed or false positive),
summed per rule into its precision. `acknowledgements` holds a channel's known, accepted
behaviours: the rule still fires and is shown, marked, and is left out of alerts and
escalations. Both are created only when absent, because startup's `create_all` may have made
them first.

Revision ID: d8f3b62a9c14
Revises: c5a91e7d2f40
Create Date: 2026-10-13 14:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.config import get_settings

revision = "d8f3b62a9c14"
down_revision = "c5a91e7d2f40"
branch_labels = None
depends_on = None

FEEDBACK = get_settings().table_name("finding_feedback")
ACKS = get_settings().table_name("acknowledgements")


def _present(table: str) -> bool:
    if op.get_context().as_sql:
        return False
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _present(FEEDBACK):
        op.create_table(
            FEEDBACK,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("job_id", sa.String(length=36), nullable=False),
            sa.Column("rule_id", sa.String(length=16), nullable=False),
            sa.Column("stream_layer", sa.String(length=16), nullable=False),
            sa.Column("variant", sa.String(length=64), nullable=True),
            sa.Column("verdict", sa.String(length=16), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(f"ix_{FEEDBACK}_job_id", FEEDBACK, ["job_id"])
        op.create_index(f"ix_{FEEDBACK}_rule_id", FEEDBACK, ["rule_id"])
    if not _present(ACKS):
        op.create_table(
            ACKS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("channel_key", sa.String(length=255), nullable=False),
            sa.Column("channel_name", sa.String(length=255), nullable=True),
            sa.Column("rule_id", sa.String(length=16), nullable=False),
            sa.Column("variant", sa.String(length=64), nullable=True),
            sa.Column("note", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(f"ix_{ACKS}_channel_key", ACKS, ["channel_key"])


def downgrade() -> None:
    op.drop_index(f"ix_{ACKS}_channel_key", table_name=ACKS)
    op.drop_table(ACKS)
    op.drop_index(f"ix_{FEEDBACK}_rule_id", table_name=FEEDBACK)
    op.drop_index(f"ix_{FEEDBACK}_job_id", table_name=FEEDBACK)
    op.drop_table(FEEDBACK)
