"""Add bot-reply quality feedback columns to inbound_classifications.

Without this, model regressions are invisible — we have no
feedback signal between "bot generated reply X" and "doctor felt
that reply was correct". A simple thumbs-up/down on each inbox
row gives ops + doctors a way to flag a bad reply for retraining
or prompt-tuning, and gives us a queryable signal for
"how often does the bot get this category wrong?".

Three columns:

    feedback_rating SMALLINT NULL
        +1 = thumbs-up (doctor / ops approves the reply)
        -1 = thumbs-down (reply was wrong / harmful / off-topic)
        NULL = no feedback yet (most rows)

    feedback_note TEXT NULL
        Optional free-form context. Useful on thumbs-down rows
        — "the bot recommended 500mg but the patient is on 250".

    feedback_by VARCHAR(128) NULL
        Operator handle for accountability — same convention as
        the other audit-trail _by columns.

    feedback_at TIMESTAMPTZ NULL
        When the rating landed.

Partial index on feedback_rating speeds the "show me all
thumbs-down rows from the last week" analytics query.

Revision ID: 20260507_0029
Revises: 20260507_0028
Create Date: 2026-05-08 11:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260507_0029"
down_revision: Union[str, None] = "20260507_0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inbound_classifications",
        sa.Column("feedback_rating", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "inbound_classifications",
        sa.Column("feedback_note", sa.Text(), nullable=True),
    )
    op.add_column(
        "inbound_classifications",
        sa.Column(
            "feedback_by", sa.String(length=128), nullable=True
        ),
    )
    op.add_column(
        "inbound_classifications",
        sa.Column(
            "feedback_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.create_index(
        "ix_inbound_classifications_feedback_rating",
        "inbound_classifications",
        ["feedback_rating", "created_at"],
        postgresql_where=sa.text("feedback_rating IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inbound_classifications_feedback_rating",
        table_name="inbound_classifications",
    )
    op.drop_column("inbound_classifications", "feedback_at")
    op.drop_column("inbound_classifications", "feedback_by")
    op.drop_column("inbound_classifications", "feedback_note")
    op.drop_column("inbound_classifications", "feedback_rating")
