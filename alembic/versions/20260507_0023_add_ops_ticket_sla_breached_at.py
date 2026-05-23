"""Add sla_breached_at to ops_tickets.

The DTO layer already computes ``is_overdue`` at request time, but
that's a live boolean — we can't distinguish "ticket has been overdue
since last week and nobody noticed" from "ticket went overdue 30
seconds ago".

A persistent first-cross timestamp gives us:

- An audit signal — the breach sweep appends a ``[SLA breached]``
  note to the ticket, and the column itself is the durable record
  even after a ticket is resolved.
- An analytics axis — ``COUNT(*) WHERE sla_breached_at IS NOT NULL``
  partitioned by category exposes which categories systematically
  miss SLA.
- A UI surface — the ops queue page can highlight breached tickets
  permanently rather than only while they're still open.

The column is NULL by default; the breach sweep stamps it on first
crossing and ``reopen()`` clears it so a re-opened ticket starts a
fresh SLA window.

Revision ID: 20260507_0023
Revises: 20260507_0022
Create Date: 2026-05-07 14:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260507_0023"
down_revision: Union[str, None] = "20260507_0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ops_tickets",
        sa.Column(
            "sla_breached_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Partial index speeds up the breach sweep's "still-unbreached"
    # filter and the future analytics queries that count breaches
    # per category. The predicate keeps the index tiny — only rows
    # currently marked as breached are stored.
    op.create_index(
        "ix_ops_tickets_sla_breached_at",
        "ops_tickets",
        ["sla_breached_at"],
        postgresql_where=sa.text("sla_breached_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ops_tickets_sla_breached_at", table_name="ops_tickets"
    )
    op.drop_column("ops_tickets", "sla_breached_at")
