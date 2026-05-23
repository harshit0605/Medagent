"""Add care_plan_exemptions table for patient-level standing-order opt-outs.

A clinician sometimes needs to exempt a specific patient from a
standing-order care plan — e.g. a diabetic on a different lab schedule
under a specialist, or a contraindicated test. Without exemptions the
sweep would keep materialising followups for that patient every time
their last lab aged out.

An exemption is identified by ``(patient_id, care_plan_id)``. Since a
patient can be exempted, then re-included, then exempted again over
their care journey, we DON'T enforce uniqueness — we add an indexed
``revoked_at`` column instead. ``revoked_at IS NULL AND
(expires_at IS NULL OR expires_at > now())`` defines an *active*
exemption; anything else is historical and visible only in the audit
view.

Revision ID: 20260503_0015
Revises: 20260503_0014
Create Date: 2026-05-03 17:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260503_0015"
down_revision: Union[str, None] = "20260503_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "care_plan_exemptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("care_plan_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("revoked_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["care_plan_id"], ["care_plans.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_care_plan_exemptions_patient",
        "care_plan_exemptions",
        ["patient_id"],
        unique=False,
    )
    op.create_index(
        "ix_care_plan_exemptions_plan",
        "care_plan_exemptions",
        ["care_plan_id"],
        unique=False,
    )
    op.create_index(
        "ix_care_plan_exemptions_active",
        "care_plan_exemptions",
        ["patient_id", "care_plan_id", "revoked_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_care_plan_exemptions_active",
        table_name="care_plan_exemptions",
    )
    op.drop_index(
        "ix_care_plan_exemptions_plan", table_name="care_plan_exemptions"
    )
    op.drop_index(
        "ix_care_plan_exemptions_patient",
        table_name="care_plan_exemptions",
    )
    op.drop_table("care_plan_exemptions")
