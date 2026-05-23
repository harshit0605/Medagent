"""Add assignment + snooze fields to ops_tickets.

  - assigned_to: free-form string (clinician handle / role) so the queue
    page can group "mine" vs "unassigned".
  - snoozed_until: timestamp the ticket should re-emerge from a snoozed
    state. NULL = not snoozed. We don't add a separate enum value —
    snoozed tickets keep their underlying status (open/acknowledged) and
    are just hidden from the active queue while the timestamp is in the
    future. When ``snoozed_until <= now()`` the ticket is back on the
    active queue.

Revision ID: 20260503_0012
Revises: 20260502_0011
Create Date: 2026-05-03 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260503_0012"
down_revision: Union[str, None] = "20260502_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ops_tickets",
        sa.Column("assigned_to", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "ops_tickets",
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ops_tickets", "snoozed_until")
    op.drop_column("ops_tickets", "assigned_to")
