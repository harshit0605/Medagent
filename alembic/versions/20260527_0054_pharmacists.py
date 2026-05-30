"""Pharmacist registry (MVP #5 — pharmacist handoff).

The only operator entity so far is Doctor; SoT MVP #5 calls for routing
refills / substitutions to a pharmacist. This adds a lightweight pharmacist
registry so refill-help and order-substitution ops tickets can be assigned to
a named pharmacist (via the existing ops_tickets.assigned_to) instead of
generic ops. No auth/login here — pharmacists act through the ops console
under the same operator-identity model as everyone else.

Revision ID: 20260527_0054
Revises: 20260527_0053
Create Date: 2026-05-27 03:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260527_0054"
down_revision: Union[str, None] = "20260527_0053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pharmacists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("pharmacy_name", sa.String(length=255), nullable=True),
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_pharmacists_active", "pharmacists", ["active"]
    )


def downgrade() -> None:
    op.drop_index("ix_pharmacists_active", table_name="pharmacists")
    op.drop_table("pharmacists")
