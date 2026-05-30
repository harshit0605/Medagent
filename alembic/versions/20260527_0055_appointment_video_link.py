"""Telehealth video link on appointments (I6).

Lets an appointment carry a video-consult link (Zoom / Meet / Jitsi). When
present, the appointment reminder includes a "Join here: <link>" line so a
telehealth patient can tap straight into the call. NULL = in-person visit
(unchanged copy).

Revision ID: 20260527_0055
Revises: 20260527_0054
Create Date: 2026-05-27 04:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260527_0055"
down_revision: Union[str, None] = "20260527_0054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column("video_link", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appointments", "video_link")
