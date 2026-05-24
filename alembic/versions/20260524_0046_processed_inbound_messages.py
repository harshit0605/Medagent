"""Add processed_inbound_messages (inbound dedupe ledger).

Meta redelivers WhatsApp webhooks whenever an ACK times out, so the same
inbound (same ``wamid``) can hit the orchestrator's /route more than once.
Without dedupe a replay re-runs the agent workflow — re-sending the reply,
re-paging clinical alerts, and re-charging LLM tokens. /route now claims each
inbound by message id via INSERT ... ON CONFLICT DO NOTHING against this
table before doing any work; a conflict means "already processed" and the
request short-circuits.

Revision ID: 20260524_0046
Revises: 20260524_0045
Create Date: 2026-05-24 00:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260524_0046"
down_revision: Union[str, None] = "20260524_0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "processed_inbound_messages",
        sa.Column("message_id", sa.String(length=255), primary_key=True),
        sa.Column("patient_id", sa.String(length=128), nullable=True),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("processed_inbound_messages")
