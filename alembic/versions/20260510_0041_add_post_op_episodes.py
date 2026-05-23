"""Add post_op_episodes table (post-op completion checklist).

Mirrors the pregnancy timeline: a bounded recovery episode anchored on the
surgery date drives day-N checklist reminders (wound check, suture removal,
follow-up visit) plus a prompt to send a wound photo for review.

``status`` is a plain String (active | ended) per the recent convention.
One active episode per patient (partial unique index).

Revision ID: 20260510_0041
Revises: 20260510_0040
Create Date: 2026-05-10 13:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260510_0041"
down_revision: Union[str, None] = "20260510_0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "post_op_episodes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "patient_id",
            sa.Integer(),
            sa.ForeignKey("patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("procedure_name", sa.String(length=255), nullable=False),
        sa.Column("surgery_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_reason", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_post_op_episodes_patient_status",
        "post_op_episodes",
        ["patient_id", "status"],
    )
    op.create_index(
        "uq_post_op_one_active_per_patient",
        "post_op_episodes",
        ["patient_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_post_op_one_active_per_patient", table_name="post_op_episodes"
    )
    op.drop_index(
        "ix_post_op_episodes_patient_status", table_name="post_op_episodes"
    )
    op.drop_table("post_op_episodes")
