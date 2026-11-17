"""batch item issues

`batch_items.issues` holds the report's `Issues` cell, one line per ERROR or CRITICAL issue,
beside `batch_items.summary`, which now holds the `Analysis Summary` cell that explains each
of them (`app/batch/summary.py`).

The column is added only when it is missing, because `create_all` builds a fresh database
from the models before anyone runs a migration.

Revision ID: f2b8d4a06c31
Revises: e1a7c3f95b20
Create Date: 2026-11-17 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.config import get_settings

revision = "f2b8d4a06c31"
down_revision = "e1a7c3f95b20"
branch_labels = None
depends_on = None

BATCH_ITEMS = get_settings().table_name("batch_items")
COLUMN = "issues"


def _existing() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(BATCH_ITEMS)}


def upgrade() -> None:
    if COLUMN not in _existing():
        op.add_column(BATCH_ITEMS, sa.Column(COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    if COLUMN in _existing():
        with op.batch_alter_table(BATCH_ITEMS) as batch:
            batch.drop_column(COLUMN)
