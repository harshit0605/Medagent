"""Add inbound_classifications for the doctor-inbox view.

For every freeform inbound that reaches the LLM compose path, we record
a classification row: what category the message falls into, a one-line
summary, an urgency tag, which handler responded, whether it was
escalated to ops, and a snippet of the bot's reply. Powers the
``/inbox`` page in the ops console so a clinician can see at a glance
what's coming in and what slipped through to ops.

Action-tap messages (``[dose-action] ...``, ``[lab-action] ...``, etc.)
intentionally skip classification — they're not freeform questions.

Revision ID: 20260503_0017
Revises: 20260503_0016
Create Date: 2026-05-03 19:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260503_0017"
down_revision: Union[str, None] = "20260503_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inbound_classifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=True),
        sa.Column("patient_phone", sa.String(length=128), nullable=False),
        sa.Column("patient_db_id", sa.Integer(), nullable=True),
        sa.Column("inbound_text", sa.Text(), nullable=True),
        sa.Column(
            "category",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "urgency",
            sa.String(length=16),
            nullable=False,
            server_default="low",
        ),
        sa.Column("handler_used", sa.String(length=64), nullable=True),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column(
            "escalated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("ticket_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["patient_db_id"], ["patients.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["ops_tickets.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_inbound_classifications_patient",
        "inbound_classifications",
        ["patient_phone", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_inbound_classifications_category",
        "inbound_classifications",
        ["category", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_inbound_classifications_urgency",
        "inbound_classifications",
        ["urgency", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_inbound_classifications_escalated",
        "inbound_classifications",
        ["escalated", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_inbound_classifications_created_at",
        "inbound_classifications",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inbound_classifications_created_at",
        table_name="inbound_classifications",
    )
    op.drop_index(
        "ix_inbound_classifications_escalated",
        table_name="inbound_classifications",
    )
    op.drop_index(
        "ix_inbound_classifications_urgency",
        table_name="inbound_classifications",
    )
    op.drop_index(
        "ix_inbound_classifications_category",
        table_name="inbound_classifications",
    )
    op.drop_index(
        "ix_inbound_classifications_patient",
        table_name="inbound_classifications",
    )
    op.drop_table("inbound_classifications")
