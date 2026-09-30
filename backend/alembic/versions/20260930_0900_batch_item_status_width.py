"""widen batch_items.status so INSUFFICIENT_DATA fits

`batch_items.status` was VARCHAR(16). A historical batch files a channel with too few days of
data as `INSUFFICIENT_DATA`, which is 17 characters, so on MySQL the first such channel was
refused with error 1406, "Data too long for column 'status'", and because the batch writes its
selected channels in one transaction the whole batch failed before it analysed anything.

SQLite never enforces a VARCHAR length, which is how this reached a MySQL deployment with the
test suite green. The column is widened to 24, and `db.session.schema_drift` now reports a
column narrower than the model so a deployment that has not run this is told to.

Revision ID: b7e2c49a1d05
Revises: a4d8e61b0c73
Create Date: 2026-09-30 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.config import get_settings

revision = "b7e2c49a1d05"
down_revision = "a4d8e61b0c73"
branch_labels = None
depends_on = None

ITEMS = get_settings().table_name("batch_items")


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    if not _is_sqlite():
        op.alter_column(
            ITEMS,
            "status",
            existing_type=sa.String(16),
            type_=sa.String(24),
            existing_nullable=False,
            # MySQL's MODIFY COLUMN drops a default it is not told to keep.
            existing_server_default=sa.text("'PENDING'"),
        )


def downgrade() -> None:
    # Narrowing would truncate or refuse every INSUFFICIENT_DATA row, so those are cleared to
    # a status that fits first; the reason stays in `error`.
    if not _is_sqlite():
        op.execute(
            sa.text(f"UPDATE {ITEMS} SET status = 'SKIPPED' WHERE CHAR_LENGTH(status) > 16")
        )
        op.alter_column(
            ITEMS,
            "status",
            existing_type=sa.String(24),
            type_=sa.String(16),
            existing_nullable=False,
            # MySQL's MODIFY COLUMN drops a default it is not told to keep.
            existing_server_default=sa.text("'PENDING'"),
        )
