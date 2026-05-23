"""Add onboarding_step to patients.

Drives the deterministic onboarding state machine (in
``services/orchestrator/onboarding_handler.py``):

    pending → needs_name → needs_cohorts → needs_consent → done

Existing rows are stamped ``done`` so historical patients aren't
re-prompted; new patients default to ``pending`` (set by
``_upsert_patient_node`` on first creation).

Revision ID: 20260502_0011
Revises: 20260502_0010
Create Date: 2026-05-02 18:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260502_0011"
down_revision: Union[str, None] = "20260502_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "patients",
        sa.Column("onboarding_step", sa.String(length=32), nullable=True),
    )
    # Existing patients have already interacted with the bot — mark them
    # ``done`` so the onboarding handler doesn't intercept their next
    # message. New rows from `_upsert_patient_node` start as ``pending``.
    op.execute(
        "UPDATE patients SET onboarding_step = 'done' WHERE onboarding_step IS NULL"
    )


def downgrade() -> None:
    op.drop_column("patients", "onboarding_step")
