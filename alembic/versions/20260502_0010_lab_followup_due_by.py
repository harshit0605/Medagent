"""Add due_by + notes to lab_followups for the reminder loop.

Lab follow-up reminders fire at T-7d, T-1d, and T+2d (overdue) relative
to ``due_by``. ``due_by`` is nullable so existing rows without a target
date keep working — the materializer simply skips them.

Revision ID: 20260502_0010
Revises: 20260502_0009
Create Date: 2026-05-02 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260502_0010"
down_revision: Union[str, None] = "20260502_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "lab_followups",
        sa.Column("due_by", sa.Date(), nullable=True),
    )
    op.add_column(
        "lab_followups",
        sa.Column("notes", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("lab_followups", "notes")
    op.drop_column("lab_followups", "due_by")
