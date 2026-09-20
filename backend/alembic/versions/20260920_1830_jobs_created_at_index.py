"""index the columns every listing orders by

Two orderings had no index behind them, and MySQL answers an unindexed ORDER BY with a
filesort that packs every selected column into `sort_buffer_size`.

`jobs.created_at` is the order of every job listing — analysed channels, aging, bulk — over
rows carrying two JSON columns and five TEXT ones. Past a few hundred rows of history the sort
no longer fits and the listing fails outright with error 1038, "Out of sort memory".

`(job_id, ts)` on the sample tables is the order the charts and the exports read a job in. The
existing `(job_id, variant, ts)` index sorts by rung first, so it cannot serve a whole job in
time order; a seven-day aging run would ask the server to sort millions of rows carrying URIs
and JSON.

Indexed, both are read in order and stopped at the limit.

Revision ID: d5e1c8b04a97
Revises: c3a9d1f47b28
Create Date: 2026-09-20 18:30:00.000000
"""

from __future__ import annotations

from alembic import op

from app.config import get_settings

revision = "d5e1c8b04a97"
down_revision = "c3a9d1f47b28"
branch_labels = None
depends_on = None

_settings = get_settings()
JOBS = _settings.table_name("jobs")
JOBS_INDEX = f"ix_{JOBS}_created_at"

SAMPLE_TABLES = tuple(
    _settings.table_name(name)
    for name in ("samples_playlist", "samples_segment", "virtual_buffer", "playlist_snapshots")
)


def upgrade() -> None:
    op.create_index(JOBS_INDEX, JOBS, ["created_at"])
    for table in SAMPLE_TABLES:
        op.create_index(f"ix_{table}_job_ts", table, ["job_id", "ts"])


def downgrade() -> None:
    for table in SAMPLE_TABLES:
        op.drop_index(f"ix_{table}_job_ts", table_name=table)
    op.drop_index(JOBS_INDEX, table_name=JOBS)
