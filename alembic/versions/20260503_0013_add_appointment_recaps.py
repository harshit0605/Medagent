"""Add appointment_recaps table for after-visit patient summaries.

A recap is the doctor-authored, LLM-polished WhatsApp message a patient
receives after their appointment. The doctor types (or pastes from EHR)
free-text notes plus structured fields — meds added/changed/stopped,
labs ordered, follow-up plan, red-flag warnings — and the system renders
a patient-friendly recap that locks in adherence and reduces "I forgot
what doctor said" forgetting.

Status lifecycle:
  draft → sent → acknowledged
                   ↘ questioned (patient tapped "I have a question")

We keep one recap per appointment (unique constraint). If a doctor needs
to revise after sending we create a follow-up note via ops_tickets, not
a second recap row.

Revision ID: 20260503_0013
Revises: 20260503_0012
Create Date: 2026-05-03 14:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260503_0013"
down_revision: Union[str, None] = "20260503_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "appointment_recaps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("doctor_id", sa.Integer(), nullable=False),
        sa.Column("doctor_notes", sa.Text(), nullable=True),
        sa.Column(
            "structured_payload",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("generated_text", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "sent",
                "acknowledged",
                "questioned",
                name="recap_status",
            ),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("sent_message_id", sa.String(length=255), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("authored_by", sa.String(length=128), nullable=True),
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
            ["appointment_id"], ["appointments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["doctor_id"], ["doctors.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "appointment_id", name="uq_appointment_recaps_appointment"
        ),
    )
    op.create_index(
        "ix_appointment_recaps_patient",
        "appointment_recaps",
        ["patient_id"],
        unique=False,
    )
    op.create_index(
        "ix_appointment_recaps_status",
        "appointment_recaps",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_appointment_recaps_status", table_name="appointment_recaps"
    )
    op.drop_index(
        "ix_appointment_recaps_patient", table_name="appointment_recaps"
    )
    op.drop_table("appointment_recaps")
    op.execute("DROP TYPE IF EXISTS recap_status")
