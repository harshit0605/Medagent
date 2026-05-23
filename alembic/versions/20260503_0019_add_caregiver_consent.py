"""Extend caregivers table with consent + cc-on-recap fields.

The original ``caregivers`` schema (migration 0001) captured contact
basics + 3 permission booleans but nothing was wired up. To safely
cc a caregiver on patient communications we need:

  - ``consent_status``: pending → confirmed (caregiver agreed to
    receive copies) / declined (explicitly opted out) / revoked
    (caregiver previously confirmed, then asked to stop). Drives the
    fan-out gate on recap send.
  - ``consent_confirmed_at`` + ``consent_confirmed_by``: audit trail.
    ``confirmed_by`` is free-text — the clinician handle who recorded
    a verbal confirmation, OR the literal ``"caregiver_yes_reply"``
    when an inbound YES from the caregiver's phone confirms consent.
  - ``notify_on_recap``: per-caregiver opt-in for post-visit recap
    copies. Defaults to ``True`` when consent_status flips to confirmed
    so the most common case is one-click; a clinician can disable for
    a specific caregiver who wants to stay listed but not get pinged.
  - ``active``: soft-delete. We keep historical caregivers (consent
    revoked, deactivated, etc.) for audit; only ``active=True`` rows
    participate in fan-out and the patient-detail UI list.

Revision ID: 20260503_0019
Revises: 20260503_0018
Create Date: 2026-05-03 21:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260503_0019"
down_revision: Union[str, None] = "20260503_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "caregivers",
        sa.Column(
            "consent_status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "caregivers",
        sa.Column(
            "consent_confirmed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "caregivers",
        sa.Column(
            "consent_confirmed_by", sa.String(length=128), nullable=True
        ),
    )
    op.add_column(
        "caregivers",
        sa.Column(
            "notify_on_recap",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "caregivers",
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.create_index(
        "ix_caregivers_patient_active",
        "caregivers",
        ["patient_id", "active"],
        unique=False,
    )
    op.create_index(
        "ix_caregivers_consent_status",
        "caregivers",
        ["consent_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_caregivers_consent_status", table_name="caregivers"
    )
    op.drop_index(
        "ix_caregivers_patient_active", table_name="caregivers"
    )
    op.drop_column("caregivers", "active")
    op.drop_column("caregivers", "notify_on_recap")
    op.drop_column("caregivers", "consent_confirmed_by")
    op.drop_column("caregivers", "consent_confirmed_at")
    op.drop_column("caregivers", "consent_status")
