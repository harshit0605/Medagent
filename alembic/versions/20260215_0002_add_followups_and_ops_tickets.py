"""add followup and ops ticket tables

Revision ID: 20260215_0002
Revises: 20260214_0001
Create Date: 2026-02-15 18:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260215_0002"
down_revision = "20260214_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE followup_status AS ENUM ('due', 'booked', 'completed', 'reviewed')")
    op.execute("CREATE TYPE ops_ticket_status AS ENUM ('open', 'acknowledged', 'resolved')")

    op.create_table(
        "lab_followups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("test_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.Enum("due", "booked", "completed", "reviewed", name="followup_status"), nullable=False, server_default="due"),
        sa.Column("booked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "appointment_followups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("clinician_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.Enum("due", "booked", "completed", "reviewed", name="followup_status"), nullable=False, server_default="due"),
        sa.Column("booked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "ops_tickets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("priority", sa.String(length=8), nullable=False),
        sa.Column("sla_minutes", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum("open", "acknowledged", "resolved", name="ops_ticket_status"), nullable=False, server_default="open"),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index("ix_lab_followups_patient_status", "lab_followups", ["patient_id", "status"], unique=False)
    op.create_index("ix_appointment_followups_patient_status", "appointment_followups", ["patient_id", "status"], unique=False)
    op.create_index("ix_ops_tickets_status_priority", "ops_tickets", ["status", "priority"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ops_tickets_status_priority", table_name="ops_tickets")
    op.drop_index("ix_appointment_followups_patient_status", table_name="appointment_followups")
    op.drop_index("ix_lab_followups_patient_status", table_name="lab_followups")

    op.drop_table("ops_tickets")
    op.drop_table("appointment_followups")
    op.drop_table("lab_followups")

    op.execute("DROP TYPE IF EXISTS ops_ticket_status")
    op.execute("DROP TYPE IF EXISTS followup_status")
