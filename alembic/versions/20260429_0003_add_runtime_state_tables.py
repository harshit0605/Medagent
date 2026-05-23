"""add runtime state tables (message log, audit, patient inbound, human queue, miss recovery, triage)

Revision ID: 20260429_0003
Revises: 20260215_0002
Create Date: 2026-04-29 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260429_0003"
down_revision: Union[str, None] = "20260215_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "message_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "direction",
            sa.Enum("inbound", "outbound", name="message_direction"),
            nullable=False,
        ),
        sa.Column("patient_id", sa.String(length=128), nullable=True),
        sa.Column(
            "payload_kind",
            sa.Enum("template", "freeform", name="message_payload_kind"),
            nullable=True,
        ),
        sa.Column("message", sa.JSON(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_message_log_direction_occurred_at",
        "message_log",
        ["direction", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_message_log_patient_id_occurred_at",
        "message_log",
        ["patient_id", "occurred_at"],
        unique=False,
    )

    op.create_table(
        "audit_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("record_type", sa.String(length=64), nullable=False),
        sa.Column("patient_id", sa.String(length=128), nullable=False),
        sa.Column("outbound_mode", sa.String(length=16), nullable=True),
        sa.Column("flow_action", sa.String(length=16), nullable=True),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column(
            "logged_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_records_patient_logged",
        "audit_records",
        ["patient_id", "logged_at"],
        unique=False,
    )

    op.create_table(
        "patient_inbound_state",
        sa.Column("patient_id", sa.String(length=128), nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("patient_id"),
    )

    op.create_table(
        "human_queue_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.String(length=128), nullable=False),
        sa.Column("medication", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("priority", sa.String(length=8), nullable=False, server_default="p2"),
        sa.Column("sla_minutes", sa.Integer(), nullable=False, server_default="120"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_human_queue_patient_med_reason",
        "human_queue_items",
        ["patient_id", "medication", "reason"],
        unique=False,
    )

    op.create_table(
        "miss_recovery_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.String(length=128), nullable=False),
        sa.Column("medication", sa.String(length=255), nullable=False),
        sa.Column(
            "reason",
            sa.Enum(
                "forgot",
                "side_effect",
                "out_of_stock",
                "confused",
                "cost",
                "other",
                name="miss_reason",
            ),
            nullable=False,
        ),
        sa.Column(
            "action",
            sa.Enum(
                "reschedule",
                "escalate_clinician",
                "refill_support",
                "human_review",
                name="miss_recovery_action",
            ),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_miss_recovery_patient_occurred",
        "miss_recovery_events",
        ["patient_id", "occurred_at"],
        unique=False,
    )

    op.create_table(
        "triage_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.String(length=128), nullable=False),
        sa.Column("cohort", sa.String(length=32), nullable=False),
        sa.Column(
            "severity",
            sa.Enum("low", "medium", "high", "critical", name="triage_severity"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column(
            "escalation_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_triage_patient_occurred",
        "triage_decisions",
        ["patient_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_triage_patient_occurred", table_name="triage_decisions")
    op.drop_table("triage_decisions")
    op.execute("DROP TYPE IF EXISTS triage_severity")

    op.drop_index("ix_miss_recovery_patient_occurred", table_name="miss_recovery_events")
    op.drop_table("miss_recovery_events")
    op.execute("DROP TYPE IF EXISTS miss_recovery_action")
    op.execute("DROP TYPE IF EXISTS miss_reason")

    op.drop_index("ix_human_queue_patient_med_reason", table_name="human_queue_items")
    op.drop_table("human_queue_items")

    op.drop_table("patient_inbound_state")

    op.drop_index("ix_audit_records_patient_logged", table_name="audit_records")
    op.drop_table("audit_records")

    op.drop_index("ix_message_log_patient_id_occurred_at", table_name="message_log")
    op.drop_index("ix_message_log_direction_occurred_at", table_name="message_log")
    op.drop_table("message_log")
    op.execute("DROP TYPE IF EXISTS message_payload_kind")
    op.execute("DROP TYPE IF EXISTS message_direction")
