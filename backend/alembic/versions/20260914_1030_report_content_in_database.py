"""store the report bytes in the database

Reports were written to the analyzer host and the row recorded only the path, so a report
did not survive the container that rendered it and a second instance could not serve one it
had not produced. The bytes now live in the reports table; `path` is kept, nullable, for the
rows an older deployment wrote and for a deployment that mirrors a copy to disk.

Revision ID: 8c41a7d9b2e5
Revises: 52e64e952e47
Create Date: 2026-09-14 10:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

from app.config import get_settings

revision = "8c41a7d9b2e5"
down_revision = "52e64e952e47"
branch_labels = None
depends_on = None

REPORTS = get_settings().table_name("reports")

# A rendered report is about a megabyte, well past MySQL's 64 KB default BLOB.
BLOB = sa.LargeBinary().with_variant(mysql.LONGBLOB(), "mysql", "mariadb")


def upgrade() -> None:
    op.add_column(REPORTS, sa.Column("content", BLOB, nullable=True))
    with op.batch_alter_table(REPORTS) as batch:
        batch.alter_column("path", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    # A row with no path cannot be served by a build that reads reports off disk, so the
    # column is filled with an empty string rather than left null.
    op.execute(sa.text(f"UPDATE {REPORTS} SET path = '' WHERE path IS NULL"))
    with op.batch_alter_table(REPORTS) as batch:
        batch.alter_column("path", existing_type=sa.Text(), nullable=False)
    op.drop_column(REPORTS, "content")
