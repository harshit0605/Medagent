"""Add supply tracking fields to regimens.

Refill reminder loop reads (supply_started_on + supply_days_initial) to
compute days-of-supply remaining. When the patient taps Refilled in
WhatsApp, the orchestrator resets ``supply_started_on`` to today — the
materializer then re-emits T-7/T-3/T-1 reminders for the next cycle.

Both fields are nullable so existing regimens without supply tracking
keep working — the materializer simply skips regimens with NULL fields.

Revision ID: 20260502_0009
Revises: 20260502_0008
Create Date: 2026-05-02 14:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260502_0009"
down_revision: Union[str, None] = "20260502_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "regimens",
        sa.Column("supply_days_initial", sa.Integer(), nullable=True),
    )
    op.add_column(
        "regimens",
        sa.Column("supply_started_on", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("regimens", "supply_started_on")
    op.drop_column("regimens", "supply_days_initial")
