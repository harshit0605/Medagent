"""Add households (multi-patient household model).

Caregivers were 1→1 with a patient. Real families need 1→many: one caregiver
(e.g. an adult child) oversees several patients (e.g. both elderly parents).
A ``households`` entity groups multiple patients; ``patients.household_id``
links a patient to their household (nullable; SET NULL on household delete so
a patient row is never lost).

``primary_caregiver_phone`` is the household's main contact (a caregiver may
already exist per-patient via the caregivers table; this is the household-level
point of contact for household digests).

Revision ID: 20260510_0042
Revises: 20260510_0041
Create Date: 2026-05-10 14:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260510_0042"
down_revision: Union[str, None] = "20260510_0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "households",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "primary_caregiver_phone", sa.String(length=32), nullable=True
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.add_column(
        "patients",
        sa.Column(
            "household_id",
            sa.Integer(),
            sa.ForeignKey("households.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_patients_household", "patients", ["household_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_patients_household", table_name="patients")
    op.drop_column("patients", "household_id")
    op.drop_table("households")
