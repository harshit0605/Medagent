"""Add input_kind to inbound_classifications.

Captures HOW the patient sent the inbound — text typed, voice note
transcribed, image with caption, structured button tap. Powers the
inbox UI's per-row input-kind badge so a clinician can see at a glance
that "this clinical_question came in as a voice note" — useful for
gauging Whisper transcription quality and for accessibility audits.

Revision ID: 20260503_0018
Revises: 20260503_0017
Create Date: 2026-05-03 20:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260503_0018"
down_revision: Union[str, None] = "20260503_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inbound_classifications",
        sa.Column(
            "input_kind",
            sa.String(length=32),
            nullable=False,
            server_default="text",
        ),
    )
    op.create_index(
        "ix_inbound_classifications_input_kind",
        "inbound_classifications",
        ["input_kind", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inbound_classifications_input_kind",
        table_name="inbound_classifications",
    )
    op.drop_column("inbound_classifications", "input_kind")
