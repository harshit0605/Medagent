"""Add care_plans table for editable cohort standing orders.

A ``care_plan`` is one standing-order rule: "every patient with cohort
flag X gets test Y every N days." V1 had three hard-coded rules in
``services/scheduler/care_gaps.py``; this migration seeds the same
three rules so behaviour is identical on first sweep, and the ops
console can extend or deactivate them without a deploy.

Cohort flags remain the existing boolean columns on ``patients``. The
``cohort_attr`` column on ``care_plans`` is restricted at the
application layer to a known allowlist (cohort_diabetes / cohort_cardiac
/ cohort_fall_risk) — that's enforced via Pydantic validation in the
orchestrator endpoints rather than a CHECK constraint, so adding a new
cohort doesn't require a migration.

Revision ID: 20260503_0014
Revises: 20260503_0013
Create Date: 2026-05-03 16:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260503_0014"
down_revision: Union[str, None] = "20260503_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "care_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cohort_attr", sa.String(length=64), nullable=False),
        sa.Column("test_name", sa.String(length=255), nullable=False),
        sa.Column("cadence_days", sa.Integer(), nullable=False),
        sa.Column(
            "due_in_days", sa.Integer(), nullable=False, server_default="14"
        ),
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cohort_attr",
            "test_name",
            name="uq_care_plans_cohort_test",
        ),
    )
    op.create_index(
        "ix_care_plans_active",
        "care_plans",
        ["active"],
        unique=False,
    )

    # Seed with the V1 hard-coded rules so the sweep behaves identically
    # the moment the migration runs.
    care_plans = sa.table(
        "care_plans",
        sa.column("cohort_attr", sa.String),
        sa.column("test_name", sa.String),
        sa.column("cadence_days", sa.Integer),
        sa.column("due_in_days", sa.Integer),
        sa.column("active", sa.Boolean),
        sa.column("notes", sa.Text),
    )
    op.bulk_insert(
        care_plans,
        [
            {
                "cohort_attr": "cohort_diabetes",
                "test_name": "HbA1c",
                "cadence_days": 180,
                "due_in_days": 14,
                "active": True,
                "notes": "Standard 6-month diabetic glycaemic monitoring.",
            },
            {
                "cohort_attr": "cohort_cardiac",
                "test_name": "Blood pressure check",
                "cadence_days": 90,
                "due_in_days": 14,
                "active": True,
                "notes": "Quarterly BP check for cardiac cohort.",
            },
            {
                "cohort_attr": "cohort_fall_risk",
                "test_name": "Vitamin D level",
                "cadence_days": 365,
                "due_in_days": 14,
                "active": True,
                "notes": "Annual Vitamin D screening; deficiency raises fall risk.",
            },
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_care_plans_active", table_name="care_plans")
    op.drop_table("care_plans")
