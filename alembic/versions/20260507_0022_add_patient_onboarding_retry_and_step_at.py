"""Add onboarding retry counter + step-transition timestamp.

Two columns that together let the onboarding handler escalate stuck
patients and recover from ones who ghosted mid-flow:

- ``onboarding_retry_count``: bumped on every re-prompt at the same
  step; reset to 0 on state advance. After three consecutive failures
  the handler creates an ``onboarding_stuck`` ops ticket so a teammate
  can reach out by call instead of leaving the patient looping.
- ``onboarding_step_at``: timestamp of the last step transition. If a
  patient hasn't advanced for ~30 days the handler resets them to
  ``pending`` on next inbound and re-greets — the alternative was a
  permanent half-onboarded row that never reaches a usable state.

Both default safely for legacy rows: retry starts at 0; ``step_at`` is
NULL (handler treats NULL as "no transition recorded yet" → no stale
reset).

Revision ID: 20260507_0022
Revises: 20260506_0021
Create Date: 2026-05-07 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260507_0022"
down_revision: Union[str, None] = "20260506_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "patients",
        sa.Column(
            "onboarding_retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "patients",
        sa.Column(
            "onboarding_step_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("patients", "onboarding_step_at")
    op.drop_column("patients", "onboarding_retry_count")
