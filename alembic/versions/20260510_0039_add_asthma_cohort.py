"""Add first-class cohort flags (asthma / post_op / pregnancy) + asthma trigger diary.

Two gaps closed:

  1. **Cohort flags.** ``patients`` only had ``cohort_diabetes`` /
     ``cohort_cardiac`` / ``cohort_fall_risk`` booleans, but the SoT defines
     six cohorts. Asthma, post-op, and pregnancy had no first-class flag — so
     broadcasts, care-plan targeting, and dashboards couldn't address them.
     We add the three missing booleans (NOT NULL, default false) and backfill
     ``cohort_pregnancy`` from any currently-active pregnancy episode
     (migration 0038) so the new flag is consistent on day one.

  2. **Asthma trigger diary.** A small ``asthma_trigger_logs`` table for the
     "trigger diary" / "voice trigger diary" flow (SoT §3C): the patient logs
     what set off their symptoms ("dust", "exercise", "cold air") and the
     clinic can spot patterns. Categorical free-text, so it's its own table
     rather than the numeric ``metric_observations`` used for rescue-puff
     counts.

Revision ID: 20260510_0039
Revises: 20260510_0038
Create Date: 2026-05-10 11:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260510_0039"
down_revision: Union[str, None] = "20260510_0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "patients",
        sa.Column(
            "cohort_asthma",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "patients",
        sa.Column(
            "cohort_post_op",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "patients",
        sa.Column(
            "cohort_pregnancy",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Backfill the pregnancy cohort flag from active pregnancy episodes so the
    # new flag agrees with the timeline engine's source of truth.
    op.execute(
        """
        UPDATE patients
        SET cohort_pregnancy = true
        WHERE id IN (
            SELECT patient_id FROM pregnancies WHERE status = 'active'
        )
        """
    )

    op.create_table(
        "asthma_trigger_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "patient_id",
            sa.Integer(),
            sa.ForeignKey("patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trigger_text", sa.String(length=255), nullable=False),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="patient_self_report",
        ),
        sa.Column(
            "logged_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_asthma_trigger_logs_patient_logged",
        "asthma_trigger_logs",
        ["patient_id", "logged_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_asthma_trigger_logs_patient_logged",
        table_name="asthma_trigger_logs",
    )
    op.drop_table("asthma_trigger_logs")
    op.drop_column("patients", "cohort_pregnancy")
    op.drop_column("patients", "cohort_post_op")
    op.drop_column("patients", "cohort_asthma")
