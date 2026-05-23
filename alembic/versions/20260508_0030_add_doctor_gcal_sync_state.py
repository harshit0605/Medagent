"""Add gcal_sync_token + gcal_last_synced_at to doctors.

Powers the inbound (Calendar → us) sync. Google's incremental
``events.list`` API returns a ``nextSyncToken`` we present on
the next call; Google then returns only events that changed
since the previous call. The token is opaque + can grow, so
``Text`` rather than ``String(255)``.

Two columns:

    gcal_sync_token TEXT NULL
        Latest opaque ``nextSyncToken`` from Google. NULL means
        "we haven't synced yet" — the next call performs a full
        initial sync (with a time window) to seed the token.

    gcal_last_synced_at TIMESTAMPTZ NULL
        Wall-clock timestamp of the last successful incremental
        pass. Used by the doctors-list UI to render "synced
        12 min ago" so ops sees the sweep is working without
        querying logs.

Token expiration: Google occasionally invalidates sync tokens
(documented as a 410 GONE response). Our sweep handles that by
clearing the column and restarting full sync on the next pass.

Revision ID: 20260508_0030
Revises: 20260507_0029
Create Date: 2026-05-08 14:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260508_0030"
down_revision: Union[str, None] = "20260507_0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "doctors",
        sa.Column("gcal_sync_token", sa.Text(), nullable=True),
    )
    op.add_column(
        "doctors",
        sa.Column(
            "gcal_last_synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("doctors", "gcal_last_synced_at")
    op.drop_column("doctors", "gcal_sync_token")
