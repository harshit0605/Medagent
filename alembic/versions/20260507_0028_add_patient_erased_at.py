"""Add ``erased_at`` to patients.

Companion to ``consent_revoked_at`` (the patient-initiated opt-out
audit) and ``bot_paused_at`` (the ops-initiated mute audit). This
column marks the patient row as having gone through the GDPR /
DPDP right-of-erasure flow:

    NULL          → patient is live (or has only had soft state
                    changes like consent revocation).
    NOT NULL      → PII has been overwritten in-place; the row +
                    its referenced clinical data are kept for
                    medical retention but are no longer tied to
                    a real person.

The actual erasure logic anonymizes ``full_name``, ``phone``,
``external_id``, related caregiver rows, message log contents,
ticket notes, and inbound classifications. This column is just
the marker so the UI + future endpoints can short-circuit
"don't surface this patient on lists / send to them / treat
them as live."

Revision ID: 20260507_0028
Revises: 20260507_0027
Create Date: 2026-05-08 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260507_0028"
down_revision: Union[str, None] = "20260507_0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "patients",
        sa.Column(
            "erased_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_patients_erased_at",
        "patients",
        ["erased_at"],
        postgresql_where=sa.text("erased_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_patients_erased_at", table_name="patients")
    op.drop_column("patients", "erased_at")
