"""Add clinical_alerts table.

Patient-safety alert queue surfaced when an inbound message
trips the triage classifier with severity ``high`` or
``critical``. Distinct from the existing ``inbound_classifications``
row (operational triage — every freeform message gets one) and
distinct from generic ``ops_tickets`` (workflow tickets — leak
forms, billing followups). Clinical alerts are the "doctor
needs to see this NOW" signal layer.

Why a dedicated table instead of overloading ops_tickets?

    1. Different lifecycle. Ops tickets resolve via "operator
       did something"; alerts resolve via "doctor reviewed and
       it's fine" or "doctor escalated to in-person care".
    2. Different SLA. Critical alerts page within 5 min;
       generic ops tickets accept 1–4h SLAs.
    3. Different audit needs. Alerts must record who reviewed
       what message at what time, and the patient said exactly
       what — for liability + chart review.
    4. Polluting ops_tickets with alert-only fields would
       make every other ticket query carry irrelevant joins.

Fields:

    severity (string)         — "high" | "critical".
                                 (medium / low don't write
                                 alert rows; they go to the
                                 normal inbox.)
    red_flags (JSON list)     — short list of trigger strings
                                 the classifier flagged
                                 ("chest pain", "shortness of
                                 breath", "suicidal ideation").
    clinical_summary (text)   — 1-line plain-language
                                 description of what the
                                 patient said, written for the
                                 doctor (≤300 chars).
    status                    — open → acknowledged → resolved.
    inbound_text              — verbatim copy of what the
                                 patient sent. Snapshot for
                                 audit; the message_log row
                                 may not survive PII erasure.

Indexes:
    (status, severity, created_at) — the open-queue view that
    sorts critical-first then by age.
    (patient_id) — patient detail page lookup.

Revision ID: 20260509_0034
Revises: 20260508_0033
Create Date: 2026-05-09 09:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260509_0034"
down_revision: Union[str, None] = "20260508_0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "clinical_alerts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "patient_id",
            sa.Integer(),
            sa.ForeignKey("patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Phone copied at insert time — the dispatcher / doctor
        # paging path keys off this even if the patient row's
        # phone is later wiped (right-of-erasure).
        sa.Column("patient_phone", sa.String(length=128), nullable=True),
        # FK to the inbound message that tripped the alert.
        # Nullable + SET NULL on cascade so message_log retention
        # policies (eventual PII erasure) don't break the alert.
        sa.Column(
            "message_id",
            sa.Integer(),
            sa.ForeignKey("message_log.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "severity",
            sa.String(length=16),
            nullable=False,
            comment="'high' or 'critical' — alerts only fire for these two",
        ),
        sa.Column(
            "red_flags",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            "clinical_summary",
            sa.Text(),
            nullable=False,
            comment="LLM-written 1-line plain-language summary for the doctor",
        ),
        sa.Column(
            "inbound_text",
            sa.Text(),
            nullable=False,
            comment="Verbatim patient message — snapshot for audit",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "llm_model",
            sa.String(length=64),
            nullable=True,
            comment="The model that produced this triage decision",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "acknowledged_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "acknowledged_by",
            sa.String(length=128),
            nullable=True,
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "resolved_by",
            sa.String(length=128),
            nullable=True,
        ),
        sa.Column(
            "resolution_notes",
            sa.Text(),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_clinical_alerts_status_severity_created",
        "clinical_alerts",
        ["status", "severity", "created_at"],
    )
    op.create_index(
        "ix_clinical_alerts_patient",
        "clinical_alerts",
        ["patient_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_clinical_alerts_patient", table_name="clinical_alerts"
    )
    op.drop_index(
        "ix_clinical_alerts_status_severity_created",
        table_name="clinical_alerts",
    )
    op.drop_table("clinical_alerts")
