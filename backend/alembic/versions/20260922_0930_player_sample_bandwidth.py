"""hold a player's bandwidth estimate, and stop it overflowing the bitrate column

`samples_player.bitrate` was a signed INT, so it held values to 2 147 483 647. The browser
player was sending two different quantities into it: the rendition bitrate on a rung switch,
which is tens of megabits, and hls.js's instantaneous bandwidth estimate on every fragment
load, which on a small segment off a nearby CDN reads in gigabits per second. A 2 816 259 958
estimate failed the INSERT with MySQL error 1264, and because the recorder writes a batch in
one transaction, one such row discarded every other sample flushed with it.

This widens `bitrate` to BIGINT and gives the estimate a column of its own. Keeping the two
apart is not only about range: they are different measurements, and the played-rung chart was
plotting throughput spikes as if the player had changed rung.

SQLite treats every integer as 64-bit and has no ALTER for a column type, so it needs neither
half of this — which is exactly why the defect reached a MySQL deployment with the whole test
suite green.

Revision ID: e9b3f207c6d4
Revises: d5e1c8b04a97
Create Date: 2026-09-22 09:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.config import get_settings

revision = "e9b3f207c6d4"
down_revision = "d5e1c8b04a97"
branch_labels = None
depends_on = None

PLAYER = get_settings().table_name("samples_player")


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.add_column(PLAYER, sa.Column("bandwidth_bps", sa.BigInteger(), nullable=True))
    if not _is_sqlite():
        op.alter_column(
            PLAYER,
            "bitrate",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=True,
        )


def downgrade() -> None:
    # The narrowing is the risky direction: a row whose bitrate is past INT range cannot be
    # represented, so it is cleared rather than silently truncated to a number that reads as a
    # real measurement. Nothing is lost that INT could have held in the first place.
    if not _is_sqlite():
        op.execute(sa.text(f"UPDATE {PLAYER} SET bitrate = NULL WHERE bitrate > 2147483647"))
        op.alter_column(
            PLAYER,
            "bitrate",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=True,
        )
    op.drop_column(PLAYER, "bandwidth_bps")
