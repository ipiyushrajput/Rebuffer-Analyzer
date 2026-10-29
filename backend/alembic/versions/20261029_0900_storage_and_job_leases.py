"""object storage keys and job leases

`reports.storage_key` names the object holding a report's bytes when they live in object
storage (local or S3) rather than in the row. `jobs.storage_prefix` is the run's folder there,
fixed when the run starts. `jobs.lease_owner`, `jobs.lease_expires_at` and
`jobs.stop_requested` let several instances share one database: a job runs on the instance
holding its lease, a lapsed lease is taken over, and a stop asked of any instance reaches the
owner.

Each column is added only when it is missing, because `create_all` builds a fresh database
from the models before anyone runs a migration.

Revision ID: e1a7c3f95b20
Revises: d8f3b62a9c14
Create Date: 2026-10-29 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.config import get_settings

revision = "e1a7c3f95b20"
down_revision = "d8f3b62a9c14"
branch_labels = None
depends_on = None

JOBS = get_settings().table_name("jobs")
REPORTS = get_settings().table_name("reports")

COLUMNS: list[tuple[str, sa.Column[object]]] = [
    (REPORTS, sa.Column("storage_key", sa.String(512), nullable=True)),
    (JOBS, sa.Column("storage_prefix", sa.String(255), nullable=True)),
    (JOBS, sa.Column("lease_owner", sa.String(128), nullable=True)),
    (JOBS, sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)),
    (
        JOBS,
        sa.Column("stop_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    ),
]


def _existing(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    for table, column in COLUMNS:
        if column.name not in _existing(table):
            op.add_column(table, column)


def downgrade() -> None:
    for table, column in reversed(COLUMNS):
        if column.name in _existing(table):
            with op.batch_alter_table(table) as batch:
                batch.drop_column(column.name)
