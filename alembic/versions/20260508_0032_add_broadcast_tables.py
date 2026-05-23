"""Add broadcast_campaigns + broadcast_sends.

Lets ops send a template to a cohort of patients — flu shot
reminders for diabetics, seasonal advice for cardiac patients,
panel-wide notifications. Each broadcast materialises N
``scheduled_events`` (one per recipient) so the existing
dispatcher + retry policy + consent gate + delivery-tracking
infrastructure carries each individual send.

``broadcast_campaigns``: campaign metadata + status.

    id, name, template_name, template_params (JSON), cohort_filter
    (JSON — ``{"cohort": "diabetes"}`` for v1), status, created_by,
    counts populated by the materialiser.

``broadcast_sends``: one row per (campaign × recipient).

    id, campaign_id, patient_id (phone), patient_db_id, status
    (pending / dispatched / skipped / failed), skip_reason
    (opted_out / paused / erased / no_phone — populated when
    skipped), scheduled_event_id (FK back to the dispatched
    event so we can join to delivery state).

Two indexes on broadcast_sends:
    - (campaign_id, status) for the per-campaign progress view
    - (patient_db_id, campaign_id) for "show all campaigns this
      patient has been part of" — useful for compliance + dedup.

Revision ID: 20260508_0032
Revises: 20260508_0031
Create Date: 2026-05-08 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260508_0032"
down_revision: Union[str, None] = "20260508_0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "broadcast_campaigns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "template_name", sa.String(length=128), nullable=False
        ),
        sa.Column(
            "template_params", sa.JSON(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "cohort_filter", sa.JSON(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "materialised_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "total_recipients",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "sent_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "skipped_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("notes", sa.Text(), nullable=True),
    )

    op.create_table(
        "broadcast_sends",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey(
                "broadcast_campaigns.id", ondelete="CASCADE"
            ),
            nullable=False,
        ),
        sa.Column(
            "patient_id", sa.String(length=128), nullable=False
        ),
        sa.Column(
            "patient_db_id",
            sa.Integer(),
            sa.ForeignKey("patients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "skip_reason", sa.String(length=64), nullable=True
        ),
        sa.Column(
            "scheduled_event_id",
            sa.Integer(),
            sa.ForeignKey(
                "scheduled_events.id", ondelete="SET NULL"
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_broadcast_sends_campaign_status",
        "broadcast_sends",
        ["campaign_id", "status"],
    )
    op.create_index(
        "ix_broadcast_sends_patient_campaign",
        "broadcast_sends",
        ["patient_db_id", "campaign_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_broadcast_sends_patient_campaign",
        table_name="broadcast_sends",
    )
    op.drop_index(
        "ix_broadcast_sends_campaign_status",
        table_name="broadcast_sends",
    )
    op.drop_table("broadcast_sends")
    op.drop_table("broadcast_campaigns")
