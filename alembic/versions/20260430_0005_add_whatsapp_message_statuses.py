"""add whatsapp_message_statuses table

Revision ID: 20260430_0005
Revises: 20260429_0004
Create Date: 2026-04-30 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260430_0005"
down_revision: Union[str, None] = "20260429_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_message_statuses",
        sa.Column("wamid", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("recipient_id", sa.String(length=128), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.Integer(), nullable=True),
        sa.Column("error_title", sa.String(length=255), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=False),
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
        sa.PrimaryKeyConstraint("wamid"),
    )
    op.create_index(
        "ix_whatsapp_status_recipient",
        "whatsapp_message_statuses",
        ["recipient_id"],
        unique=False,
    )
    op.create_index(
        "ix_whatsapp_status_updated_at",
        "whatsapp_message_statuses",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_whatsapp_status_updated_at", table_name="whatsapp_message_statuses")
    op.drop_index("ix_whatsapp_status_recipient", table_name="whatsapp_message_statuses")
    op.drop_table("whatsapp_message_statuses")
