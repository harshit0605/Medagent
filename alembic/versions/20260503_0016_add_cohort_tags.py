"""Add clinician-authored cohort tags + patient assignments.

Three changes:

  1. ``cohort_tags`` — clinician-authored cohort labels, e.g. "Post-MI",
     "Pregnancy 3T", "Renal CKD-3+". Slug is the URL-safe identifier
     used for routing; label is the human-friendly display name.
  2. ``patient_cohort_tags`` — M2M between patients and tags, with
     ``assigned_by`` + ``assigned_at`` so a clinician can audit who
     placed a patient in a cohort and when. Uniqueness on
     ``(patient_id, cohort_tag_id)`` so the same patient can't be
     re-assigned to the same tag twice (use deactivation instead).
  3. ``care_plans`` gains a nullable ``cohort_tag_id`` FK. Existing
     ``cohort_attr`` becomes nullable. Each plan must reference exactly
     one of the two — the application layer enforces that constraint
     since it depends on application-level allowlists for cohort_attr.

Existing 3 boolean cohorts on ``patients`` (cohort_diabetes /
cohort_cardiac / cohort_fall_risk) are intentionally left in place.
Migrating them to tags would touch every cohort-querying surface in
the codebase (onboarding, patient summaries, list filters). That's a
follow-up data migration; for now legacy + tag plans coexist and the
sweep handles both.

Revision ID: 20260503_0016
Revises: 20260503_0015
Create Date: 2026-05-03 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260503_0016"
down_revision: Union[str, None] = "20260503_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cohort_tags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
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
        sa.UniqueConstraint("slug", name="uq_cohort_tags_slug"),
    )
    op.create_index(
        "ix_cohort_tags_active",
        "cohort_tags",
        ["active"],
        unique=False,
    )

    op.create_table(
        "patient_cohort_tags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("cohort_tag_id", sa.Integer(), nullable=False),
        sa.Column("assigned_by", sa.String(length=128), nullable=True),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["cohort_tag_id"], ["cohort_tags.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "patient_id",
            "cohort_tag_id",
            name="uq_patient_cohort_tags_assignment",
        ),
    )
    op.create_index(
        "ix_patient_cohort_tags_patient",
        "patient_cohort_tags",
        ["patient_id"],
        unique=False,
    )
    op.create_index(
        "ix_patient_cohort_tags_tag",
        "patient_cohort_tags",
        ["cohort_tag_id"],
        unique=False,
    )

    # Extend care_plans with the tag-based cohort path. Plans either
    # have a cohort_attr (legacy boolean column on patients) OR a
    # cohort_tag_id (tag-based); never both. The application layer
    # enforces the XOR constraint.
    op.add_column(
        "care_plans",
        sa.Column(
            "cohort_tag_id",
            sa.Integer(),
            sa.ForeignKey("cohort_tags.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.alter_column(
        "care_plans",
        "cohort_attr",
        existing_type=sa.String(length=64),
        nullable=True,
    )
    op.create_index(
        "ix_care_plans_cohort_tag_id",
        "care_plans",
        ["cohort_tag_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_care_plans_cohort_tag_id", table_name="care_plans")
    op.alter_column(
        "care_plans",
        "cohort_attr",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.drop_column("care_plans", "cohort_tag_id")

    op.drop_index(
        "ix_patient_cohort_tags_tag", table_name="patient_cohort_tags"
    )
    op.drop_index(
        "ix_patient_cohort_tags_patient", table_name="patient_cohort_tags"
    )
    op.drop_table("patient_cohort_tags")

    op.drop_index("ix_cohort_tags_active", table_name="cohort_tags")
    op.drop_table("cohort_tags")
