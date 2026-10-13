"""the poll log: every playlist fetch of every run, bodies stored once per job

`manifest_polls` holds one row per playlist fetch — URL, final URL, status, response headers,
the redirect chain, the timing split and the hash of the body. `manifest_bodies` holds each
distinct body once per job, zlib-compressed. Together they are the network record a finding
links to (the window as it stood when the rule fired) and the network log an operator
downloads.

Both tables are created only when absent, because startup's `create_all` may have made them
first.

Revision ID: c5a91e7d2f40
Revises: b7e2c49a1d05
Create Date: 2026-10-13 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

from app.config import get_settings

revision = "c5a91e7d2f40"
down_revision = "b7e2c49a1d05"
branch_labels = None
depends_on = None

POLLS = get_settings().table_name("manifest_polls")
BODIES = get_settings().table_name("manifest_bodies")

_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_BLOB = sa.LargeBinary().with_variant(mysql.LONGBLOB(), "mysql", "mariadb")


def _present(table: str) -> bool:
    """True when startup's `create_all` made the table before this revision ran.

    `alembic upgrade --sql` writes the DDL without a database to look at, so it is always
    written in full there.
    """
    if op.get_context().as_sql:
        return False
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _present(POLLS):
        op.create_table(
            POLLS,
            sa.Column("id", _ID, primary_key=True, autoincrement=True),
            sa.Column("job_id", sa.String(length=36), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("stream_layer", sa.String(length=16), nullable=False),
            sa.Column("variant", sa.String(length=64), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("final_url", sa.Text(), nullable=True),
            sa.Column("status", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("ttfb_ms", sa.Float(), nullable=True),
            sa.Column("total_ms", sa.Float(), nullable=False, server_default="0"),
            sa.Column("bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("msn", _ID, nullable=True),
            sa.Column("last_msn", _ID, nullable=True),
            sa.Column("headers", sa.JSON(), nullable=True),
            sa.Column("hops", sa.JSON(), nullable=True),
            sa.Column("timings", sa.JSON(), nullable=True),
            sa.Column("body_hash", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.create_index(f"ix_{POLLS}_job_id", POLLS, ["job_id"])
        op.create_index(f"ix_{POLLS}_ts", POLLS, ["ts"])
        op.create_index(f"ix_{POLLS}_job_seq", POLLS, ["job_id", "seq"])
        op.create_index(f"ix_{POLLS}_job_variant_ts", POLLS, ["job_id", "variant", "ts"])
        op.create_index(f"ix_{POLLS}_job_hash", POLLS, ["job_id", "body_hash"])

    if not _present(BODIES):
        op.create_table(
            BODIES,
            sa.Column("id", _ID, primary_key=True, autoincrement=True),
            sa.Column("job_id", sa.String(length=36), nullable=False),
            sa.Column("body_hash", sa.String(length=64), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("body", _BLOB, nullable=False),
            sa.UniqueConstraint("job_id", "body_hash", name=f"uq_{BODIES}_job_hash"),
        )
        op.create_index(f"ix_{BODIES}_job_id", BODIES, ["job_id"])


def downgrade() -> None:
    op.drop_index(f"ix_{BODIES}_job_id", table_name=BODIES)
    op.drop_table(BODIES)
    for name in ("job_hash", "job_variant_ts", "job_seq", "ts", "job_id"):
        op.drop_index(f"ix_{POLLS}_{name}", table_name=POLLS)
    op.drop_table(POLLS)
