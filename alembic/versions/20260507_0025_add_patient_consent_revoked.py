"""Add consent_revoked_at + consent_revoked_reason to patients.

``consent_sms`` already gates outbound, but flipping it to false on a
STOP keyword loses the audit trail (when did they opt out, what was
the trigger?). The two new columns capture:

- ``consent_revoked_at``: timestamp the patient opted out. Cleared
  when they later opt back in via START. Lets ops chart opt-out
  rates, see how long an opted-out patient has been silent, and
  satisfy data-subject-access requests.
- ``consent_revoked_reason``: free-form reason string, defaulting to
  ``"patient_stop_keyword"`` when the patient sent STOP themselves.
  Reserved values: ``"ops_manual"`` (set by an operator), ``"bounce"``
  (auto-revoked after persistent delivery failures — future work).

Both NULLable — most patients have never opted out. The columns are
informational; the actual gate is still ``consent_sms``.

Revision ID: 20260507_0025
Revises: 20260507_0024
Create Date: 2026-05-07 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260507_0025"
down_revision: Union[str, None] = "20260507_0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "patients",
        sa.Column(
            "consent_revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "patients",
        sa.Column(
            "consent_revoked_reason",
            sa.String(length=255),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("patients", "consent_revoked_reason")
    op.drop_column("patients", "consent_revoked_at")
