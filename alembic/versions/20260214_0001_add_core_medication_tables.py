"""add core medication data model tables

Revision ID: 20260214_0001
Revises:
Create Date: 2026-02-14 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260214_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "patients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("consent_sms", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("consent_voice", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("consent_email", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("cohort_fall_risk", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("cohort_diabetes", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("cohort_cardiac", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id"),
    )
    op.create_index("ix_patients_phone", "patients", ["phone"], unique=True)

    op.create_table(
        "caregivers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("relationship_to_patient", sa.String(length=64), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("permission_view_phi", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "permission_manage_medications",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("permission_manage_orders", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "prescriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("source_upload_url", sa.Text(), nullable=False),
        sa.Column("parsed_payload", sa.JSON(), nullable=False),
        sa.Column(
            "human_verification_status",
            sa.Enum("pending", "verified", "rejected", name="verification_status"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "regimens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("prescription_id", sa.Integer(), nullable=True),
        sa.Column("medication_name", sa.String(length=255), nullable=False),
        sa.Column("dose", sa.String(length=128), nullable=False),
        sa.Column("schedule", sa.JSON(), nullable=False),
        sa.Column("strict_timing", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("starts_on", sa.Date(), nullable=True),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["prescription_id"], ["prescriptions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "adherence_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("regimen_id", sa.Integer(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum("scheduled", "taken", "missed", "skipped", "delayed", name="adherence_status"),
            nullable=False,
            server_default="scheduled",
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_metadata", sa.JSON(), nullable=False),
        sa.Column("channel_message_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["regimen_id"], ["regimens.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_adherence_events_patient_id_scheduled_at",
        "adherence_events",
        ["patient_id", "scheduled_at"],
        unique=False,
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("adherence_event_id", sa.Integer(), nullable=True),
        sa.Column("severity", sa.Enum("low", "medium", "high", "critical", name="alert_severity"), nullable=False),
        sa.Column(
            "lifecycle_status",
            sa.Enum("open", "acknowledged", "resolved", "closed", name="alert_lifecycle"),
            nullable=False,
            server_default="open",
        ),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assignee", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["adherence_event_id"], ["adherence_events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_alerts_patient_opened_closed",
        "alerts",
        ["patient_id", "opened_at", "closed_at"],
        unique=False,
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("partner", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "processing", "shipped", "delivered", "canceled", name="order_status"),
            nullable=False,
        ),
        sa.Column("partner_order_id", sa.String(length=128), nullable=True),
        sa.Column("receipt_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False, server_default="en"),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("variables", sa.JSON(), nullable=False),
        sa.Column(
            "category",
            sa.Enum("utility", "marketing", "transactional", name="template_category"),
            nullable=False,
            server_default="utility",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "language", name="uq_templates_name_language"),
    )

    template_table = sa.table(
        "templates",
        sa.column("name", sa.String),
        sa.column("language", sa.String),
        sa.column("body", sa.Text),
        sa.column("variables", sa.JSON),
        sa.column("category", sa.String),
    )

    op.bulk_insert(
        template_table,
        [
            {
                "name": "dose_reminder_v1",
                "language": "en",
                "body": "Hi {{patient_first_name}}, it's time to take {{medication_name}} ({{dose}}).",
                "variables": ["patient_first_name", "medication_name", "dose"],
                "category": "utility",
            },
            {
                "name": "refill_due_v1",
                "language": "en",
                "body": "Your refill for {{medication_name}} is due on {{refill_date}}.",
                "variables": ["medication_name", "refill_date"],
                "category": "utility",
            },
            {
                "name": "missed_dose_followup_v1",
                "language": "en",
                "body": "We noticed a missed dose of {{medication_name}} at {{scheduled_time}}. Need help?",
                "variables": ["medication_name", "scheduled_time"],
                "category": "utility",
            },
            {
                "name": "caregiver_escalation_v1",
                "language": "en",
                "body": "{{patient_name}} may need support with {{medication_name}} adherence.",
                "variables": ["patient_name", "medication_name"],
                "category": "utility",
            },
            {
                "name": "order_status_update_v1",
                "language": "en",
                "body": "Your order {{order_id}} is now {{order_status}}.",
                "variables": ["order_id", "order_status"],
                "category": "utility",
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("templates")
    op.drop_table("orders")

    op.drop_index("ix_alerts_patient_opened_closed", table_name="alerts")
    op.drop_table("alerts")

    op.drop_index("ix_adherence_events_patient_id_scheduled_at", table_name="adherence_events")
    op.drop_table("adherence_events")

    op.drop_table("regimens")
    op.drop_table("prescriptions")
    op.drop_table("caregivers")

    op.drop_index("ix_patients_phone", table_name="patients")
    op.drop_table("patients")

    op.execute("DROP TYPE IF EXISTS template_category")
    op.execute("DROP TYPE IF EXISTS order_status")
    op.execute("DROP TYPE IF EXISTS alert_lifecycle")
    op.execute("DROP TYPE IF EXISTS alert_severity")
    op.execute("DROP TYPE IF EXISTS adherence_status")
    op.execute("DROP TYPE IF EXISTS verification_status")
