"""Add wamid to message_log so outbound rows can join to whatsapp_message_statuses.

The capture pipeline (Next.js webhook → gateway → upsert into
``whatsapp_message_statuses``) has been live for a while — every
delivery / read / failure event lands in that table keyed by
``wamid``.

What's been missing is the bridge. ``message_log.append_outbound`` was
stuffing the wamid inside ``message.payload._wamid`` (a JSON field),
so any "what fraction of outbound got delivered?" query had to either:

  - LATERAL into the JSON path (slow, no index), or
  - re-extract every row in Python (correctness via N+1).

A first-class ``wamid`` column with a partial index makes the join
cheap and unlocks the per-template / per-payload-kind delivery
metrics the dashboard now wants.

The column is nullable because:

  - Inbound rows obviously never have one.
  - Outbound rows where the gateway send failed *before* Meta replied
    (timeout / 5xx) also have no wamid; we keep the row anyway because
    the failure itself is signal worth retaining.

Partial index ``WHERE wamid IS NOT NULL`` keeps the index size in
proportion to outbound volume rather than total log volume.

Revision ID: 20260507_0024
Revises: 20260507_0023
Create Date: 2026-05-07 16:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260507_0024"
down_revision: Union[str, None] = "20260507_0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "message_log",
        sa.Column("wamid", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_message_log_wamid",
        "message_log",
        ["wamid"],
        postgresql_where=sa.text("wamid IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_message_log_wamid", table_name="message_log")
    op.drop_column("message_log", "wamid")
