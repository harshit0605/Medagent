"""Add scheduled_events.idempotency_key for race-safe materialization.

The pregnancy + post-op materializers deduped only in Python (query pending
events, skip ones already queued). That's safe under a single scheduler
process but races under concurrent / multi-instance ticks — two ticks could
both pass the Python check and enqueue duplicate medical reminders. (The dose
materializer is already race-safe via a unique constraint on its paired
adherence_events row; these direct-enqueue materializers had no such backstop.)

Add a nullable ``idempotency_key`` column + a PARTIAL unique index (unique only
where the key is set). Materializers compute a deterministic key per one-shot
reminder (e.g. ``preg_milestone:{pregnancy_id}:{milestone_key}``) and enqueue
with ON CONFLICT DO NOTHING, so a concurrent duplicate is dropped at the DB.
Recurring events (dose/refill) leave the key NULL and are unaffected.

Revision ID: 20260510_0044
Revises: 20260510_0043
Create Date: 2026-05-10 16:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260510_0044"
down_revision: Union[str, None] = "20260510_0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "scheduled_events",
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "uq_scheduled_events_idempotency_key",
        "scheduled_events",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_scheduled_events_idempotency_key",
        table_name="scheduled_events",
    )
    op.drop_column("scheduled_events", "idempotency_key")
