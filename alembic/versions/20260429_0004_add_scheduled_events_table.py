"""add scheduled_events table

Revision ID: 20260429_0004
Revises: 20260429_0003
Create Date: 2026-04-29 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260429_0004"
down_revision: Union[str, None] = "20260429_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scheduled_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("patient_id", sa.String(length=128), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "dispatched",
                "skipped",
                "failed",
                name="scheduled_event_status",
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scheduled_events_status_scheduled_for",
        "scheduled_events",
        ["status", "scheduled_for"],
        unique=False,
    )
    op.create_index(
        "ix_scheduled_events_patient",
        "scheduled_events",
        ["patient_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_events_patient", table_name="scheduled_events")
    op.drop_index(
        "ix_scheduled_events_status_scheduled_for", table_name="scheduled_events"
    )
    op.drop_table("scheduled_events")
    op.execute("DROP TYPE IF EXISTS scheduled_event_status")
