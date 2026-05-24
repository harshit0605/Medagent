"""Composite index on ops_tickets(patient_id, category, status).

``ops_tickets`` had only the ``(status, priority)`` queue index and the
``sla_breached_at`` marker — nothing supporting the patient-scoped lookups
that run on the hottest paths:

  * ``find_open_for_patient_category(patient_id, category, status)`` — called
    on every missed-dose / recap / refill / care-gap escalation to dedupe
    tickets.
  * ``list_for_patient`` / ``list_for_patient_by_category`` — the patient
    detail page + the patients-list count.

All three were sequential scans. A composite ``(patient_id, category,
status)`` serves the category paths directly and the patient-only path via
its leading column.

Revision ID: 20260524_0045
Revises: 20260510_0044
Create Date: 2026-05-24 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260524_0045"
down_revision: Union[str, None] = "20260510_0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_ops_tickets_patient_category_status",
        "ops_tickets",
        ["patient_id", "category", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ops_tickets_patient_category_status",
        table_name="ops_tickets",
    )
