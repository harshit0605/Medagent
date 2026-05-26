"""Postpartum fields on the pregnancies table.

The pregnancy episode now extends past delivery into a structured postpartum
phase: the same row carries the delivery outcome + a separate ``postpartum_*``
lifecycle so we can run a parallel materializer for PP-specific reminders
(early PP check, mental-health screen, 6-week visit + contraception counsel,
pediatric vaccine nudges).

Schema additions:
  * ``delivery_date``       — DATE, when birth (or loss) occurred
  * ``birth_outcome``       — STRING(32): delivered / miscarriage / stillbirth
                              / termination / unknown
  * ``postpartum_active``   — BOOL, defaults FALSE; flipped TRUE only when
                              outcome == delivered AND delivery_date is set
  * ``postpartum_ended_at`` — DATETIME, when PP phase closed
  * ``postpartum_ended_reason`` — STRING(255), free-text close reason

A partial unique index enforces at most one row with ``postpartum_active=true``
per patient (mirrors the same shape used for ``status='active'`` in 0038).

Revision ID: 20260526_0048
Revises: 20260526_0047
Create Date: 2026-05-26 01:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260526_0048"
down_revision: Union[str, None] = "20260526_0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pregnancies",
        sa.Column("delivery_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "pregnancies",
        sa.Column("birth_outcome", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "pregnancies",
        sa.Column(
            "postpartum_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "pregnancies",
        sa.Column(
            "postpartum_ended_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "pregnancies",
        sa.Column(
            "postpartum_ended_reason",
            sa.String(length=255),
            nullable=True,
        ),
    )
    # Partial unique index — at most one active postpartum episode per patient
    # at any time. Patients with no PP phase have no row matching this WHERE,
    # so the index is small.
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_pregnancies_patient_postpartum_active "
            "ON pregnancies (patient_id) WHERE postpartum_active = true"
        )
    )
    # Sweep entry-point index: list_postpartum_active scans WHERE
    # postpartum_active = true. Selective enough to want a dedicated index.
    op.execute(
        sa.text(
            "CREATE INDEX ix_pregnancies_postpartum_active "
            "ON pregnancies (postpartum_active) WHERE postpartum_active = true"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP INDEX IF EXISTS uq_pregnancies_patient_postpartum_active"
        )
    )
    op.execute(
        sa.text("DROP INDEX IF EXISTS ix_pregnancies_postpartum_active")
    )
    op.drop_column("pregnancies", "postpartum_ended_reason")
    op.drop_column("pregnancies", "postpartum_ended_at")
    op.drop_column("pregnancies", "postpartum_active")
    op.drop_column("pregnancies", "birth_outcome")
    op.drop_column("pregnancies", "delivery_date")
