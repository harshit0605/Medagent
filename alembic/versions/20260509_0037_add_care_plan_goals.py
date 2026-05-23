"""Add care_plan_goals + metric_observations.

Closes the doctor workflow loop: "is this patient actually
getting better?". Doctor sets a quantitative goal on the
patient (``HbA1c < 7.0%``, ``BP_systolic < 130 mmHg``,
``weight < 80kg``); ops / labs / future patient-self-report
ingestion records observations against it; the patient detail
page shows the trend.

Two tables instead of one denormalised observations-only
table because:

    1. Goal lifecycle (active → achieved → inactive) is
       independent of any single observation. Storing the
       target on every observation row would double-write +
       drift on target changes.
    2. A patient can have observations *without* a goal
       (one-off lab result) — keeping observations.goal_id
       nullable preserves that history.
    3. The trend chart query is per-goal and can stop at
       50 most-recent observations; a single big table would
       force a more expensive group-by-metric_key query for
       the same view.

``care_plan_goals``:
    - patient_id FK
    - metric_key (short string — "hba1c", "bp_systolic",
      "weight_kg", "fasting_glucose"). Free-form for v1 —
      we don't enforce an enum because the cohort of useful
      metrics expands faster than migrations.
    - metric_label — friendly display name set at creation
      time. Decoupled from metric_key so the doctor's
      preferred wording is preserved.
    - target_value NUMERIC(8,3) — clinical precision. HbA1c
      is one decimal, weight is one or two; integer
      comparators (BP) round-trip fine through NUMERIC.
    - comparator — "less_than" | "greater_than" |
      "between" (range). v1 ships less_than + greater_than
      only; "between" reserved for future range goals.
    - target_unit — printable unit ("%", "mg/dL", "mmHg",
      "kg"). The metric_key alone isn't always
      unambiguous (units for "glucose" depend on regional
      conventions).
    - status — "active" | "achieved" | "inactive".
    - ends_on — optional target date.
    - created_by, notes.

``metric_observations``:
    - patient_id FK
    - goal_id FK (nullable, SET NULL on goal delete — keeps
      historical observations even if the goal is removed)
    - metric_key + value + unit (denormalised so an
      observation row stands on its own without joining
      back to the goal)
    - observed_at — when the measurement was *taken*, not
      when it was recorded. The doctor entering a lab
      result from yesterday backfills observed_at, and the
      trend chart sorts by observed_at.
    - source — "manual" | "patient_self_report" | "lab"
      | "device". Drives downstream trust/weight scoring.
    - recorded_by, notes.

Indexes:
    - care_plan_goals (patient_id, status, created_at) —
      "show me this patient's active goals"
    - metric_observations (patient_id, metric_key,
      observed_at DESC) — trend chart query
    - metric_observations (goal_id, observed_at DESC) —
      per-goal observation list

Revision ID: 20260509_0037
Revises: 20260509_0036
Create Date: 2026-05-09 14:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260509_0037"
down_revision: Union[str, None] = "20260509_0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "care_plan_goals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "patient_id",
            sa.Integer(),
            sa.ForeignKey("patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("metric_key", sa.String(length=64), nullable=False),
        sa.Column("metric_label", sa.String(length=128), nullable=False),
        sa.Column(
            "target_value", sa.Numeric(precision=8, scale=3), nullable=False
        ),
        sa.Column(
            "comparator",
            sa.String(length=32),
            nullable=False,
            server_default="less_than",
        ),
        sa.Column("target_unit", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
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
    op.create_index(
        "ix_care_plan_goals_patient_status",
        "care_plan_goals",
        ["patient_id", "status", "created_at"],
    )

    op.create_table(
        "metric_observations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "patient_id",
            sa.Integer(),
            sa.ForeignKey("patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "goal_id",
            sa.Integer(),
            sa.ForeignKey("care_plan_goals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("metric_key", sa.String(length=64), nullable=False),
        sa.Column(
            "value", sa.Numeric(precision=10, scale=3), nullable=False
        ),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="manual",
        ),
        sa.Column("recorded_by", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_metric_observations_patient_metric",
        "metric_observations",
        ["patient_id", "metric_key", "observed_at"],
    )
    op.create_index(
        "ix_metric_observations_goal",
        "metric_observations",
        ["goal_id", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_metric_observations_goal",
        table_name="metric_observations",
    )
    op.drop_index(
        "ix_metric_observations_patient_metric",
        table_name="metric_observations",
    )
    op.drop_table("metric_observations")
    op.drop_index(
        "ix_care_plan_goals_patient_status",
        table_name="care_plan_goals",
    )
    op.drop_table("care_plan_goals")
