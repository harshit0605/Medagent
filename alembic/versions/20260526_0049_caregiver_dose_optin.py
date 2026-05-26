"""Caregiver opt-in for dose-reminder fan-out.

Adds ``notify_on_dose_reminder`` to caregivers — opt-in per caregiver row
(default OFF) so existing rows don't immediately start receiving fan-out
sends. Mirror of the existing ``notify_on_recap`` flag.

Dose-reminder fan-out is also globally gated by the
``CAREGIVER_DOSE_FANOUT_ENABLED`` env flag on the dispatcher, so even with
the column flipped the dispatcher only fires fan-outs when ops has confirmed
the Meta-approved caregiver template is live.

Revision ID: 20260526_0049
Revises: 20260526_0048
Create Date: 2026-05-26 02:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260526_0049"
down_revision: Union[str, None] = "20260526_0048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "caregivers",
        sa.Column(
            "notify_on_dose_reminder",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("caregivers", "notify_on_dose_reminder")
