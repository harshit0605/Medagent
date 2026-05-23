"""Add bot_paused_at + bot_paused_reason + bot_paused_by to patients.

Distinct from ``consent_revoked_*`` (the opt-out audit trail) by
intent and ownership:

    - opt-out      = patient-initiated. Sends an ack to the patient.
                     Cleared via patient sending START.
    - bot pause    = ops-initiated. NO patient-facing ack. Cleared
                     via ops UI button. Patient's consent_sms is
                     unchanged — we're temporarily muting the bot,
                     not revoking the patient's permission.

Use case: ops gets a complaint, suspects the bot said something
concerning, or needs to investigate before any further outbound
fires. With opt-out as the only tool, the only way to halt outbound
is to revoke consent — which sends a "you've opted out" message the
patient never asked for, and confuses everyone.

The columns are NULL by default; only the small subset of patients
who've ever been paused carry values. ``bot_paused_by`` records the
operator handle so the audit is attributable.

Revision ID: 20260507_0026
Revises: 20260507_0025
Create Date: 2026-05-07 22:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260507_0026"
down_revision: Union[str, None] = "20260507_0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "patients",
        sa.Column(
            "bot_paused_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "patients",
        sa.Column(
            "bot_paused_reason",
            sa.String(length=255),
            nullable=True,
        ),
    )
    op.add_column(
        "patients",
        sa.Column(
            "bot_paused_by",
            sa.String(length=128),
            nullable=True,
        ),
    )
    # Partial index so the dispatcher gate's "is this patient paused?"
    # check is O(log n) on the paused subset rather than scanning all
    # patients. Keeps the index tiny — paused patients are an
    # exception, not the rule.
    op.create_index(
        "ix_patients_bot_paused_at",
        "patients",
        ["bot_paused_at"],
        postgresql_where=sa.text("bot_paused_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_patients_bot_paused_at", table_name="patients")
    op.drop_column("patients", "bot_paused_by")
    op.drop_column("patients", "bot_paused_reason")
    op.drop_column("patients", "bot_paused_at")
