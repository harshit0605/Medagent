"""add doctors table for Google Calendar integration

Revision ID: 20260430_0006
Revises: 20260430_0005
Create Date: 2026-04-30 14:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260430_0006"
down_revision: Union[str, None] = "20260430_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "doctors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column(
            "calendar_id",
            sa.String(length=255),
            nullable=False,
            server_default="primary",
        ),
        sa.Column(
            "oauth_status",
            sa.Enum(
                "disconnected",
                "connected",
                "expired",
                "revoked",
                name="doctor_oauth_status",
            ),
            nullable=False,
            server_default="disconnected",
        ),
        sa.Column("oauth_refresh_token_enc", sa.Text(), nullable=True),
        sa.Column("oauth_access_token", sa.Text(), nullable=True),
        sa.Column("oauth_access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oauth_scopes", sa.Text(), nullable=True),
        sa.Column("google_user_id", sa.String(length=64), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_doctors_email", "doctors", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_doctors_email", table_name="doctors")
    op.drop_table("doctors")
    op.execute("DROP TYPE IF EXISTS doctor_oauth_status")
