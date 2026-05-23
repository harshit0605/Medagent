"""Add pregnancies table (pregnancy timeline engine).

The pregnancy cohort needs a per-patient timeline anchored on the last
menstrual period (LMP) so the milestone materializer can compute gestational
age and schedule trimester-appropriate reminders (ANC visits, labs, scans,
supplements) plus a weekly check-in.

A dedicated table (rather than columns on ``patients``) because:

    1. Lifecycle. A pregnancy is a bounded episode (active → ended) that
       repeats over a patient's life. Columns on ``patients`` would force a
       wipe-and-reset on each new pregnancy and lose history.
    2. The milestone sweep walks ``WHERE status='active'`` exactly like the
       lab-followup materializer walks open follow-ups — a per-episode row is
       the natural unit to scan and dedupe against.
    3. ``patient_id``-scoped, CASCADE on patient delete — right-of-erasure
       (slice 19) drops the timeline with the patient automatically.

``pregnancies``:
    - patient_id FK (CASCADE)
    - lmp_date — last menstrual period (Date, nullable). Either this OR ``edd``
      must be set; the engine derives the missing one (EDD = LMP + 280 days,
      Naegele's rule). Nullable individually so a patient who only knows their
      due date can still be tracked.
    - edd — estimated due date (Date, nullable).
    - status — "active" | "ended". Plain String (not a PG enum) following the
      care_plan_goals convention — clinical states churn faster than enum
      migrations ship.
    - ended_at / ended_reason — set when the episode closes (delivered /
      miscarried / corrected). Kept for history; the sweep ignores ended rows.
    - notes — free-form clinician note.

A partial unique index enforces at most ONE active pregnancy per patient at
the DB level (race-safe) while still allowing many historical ended rows.

Revision ID: 20260510_0038
Revises: 20260509_0037
Create Date: 2026-05-10 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260510_0038"
down_revision: Union[str, None] = "20260509_0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pregnancies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "patient_id",
            sa.Integer(),
            sa.ForeignKey("patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lmp_date", sa.Date(), nullable=True),
        sa.Column("edd", sa.Date(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_reason", sa.String(length=255), nullable=True),
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
        "ix_pregnancies_patient_status",
        "pregnancies",
        ["patient_id", "status"],
    )
    # At most one ACTIVE pregnancy per patient (race-safe at the DB level).
    # Partial index leaves historical ended rows unconstrained.
    op.create_index(
        "uq_pregnancies_one_active_per_patient",
        "pregnancies",
        ["patient_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_pregnancies_one_active_per_patient",
        table_name="pregnancies",
    )
    op.drop_index(
        "ix_pregnancies_patient_status",
        table_name="pregnancies",
    )
    op.drop_table("pregnancies")
