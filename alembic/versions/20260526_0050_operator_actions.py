"""Per-operator action audit log.

The workflow ``audit_records`` table captures workflow decisions (why this
inbound was classified as urgency=high, why the dispatcher chose template
mode, etc.) — operator-scoped. To answer "which operator triggered which
PHI-touching action?" we previously had to join into ``audit_records.details``
JSONB and grep for ``actor`` keys. That's slow and brittle.

This table is operator-centric: every privileged action (DSAR export,
erasure trigger, ticket lifecycle change, care-plan exemption grant, etc.)
writes one row keyed by ``(operator_id, action, target_type, target_id)``.
Append-only; the only index of meaning is ``(operator_id, logged_at DESC)``
for the per-operator activity view.

Schema:
  * id              — PK
  * operator_id     — caller-asserted (X-Ops-Actor) or signed identity
  * action          — short code (patient_export / patient_erasure /
                      ticket_resolve / exemption_grant / ...)
  * target_type     — patient / ticket / care_plan / etc.
  * target_id       — string (since some targets are phone-keyed, not int)
  * details         — JSONB, freeform per-action context
  * logged_at       — UTC timestamp, server-default now()

Revision ID: 20260526_0050
Revises: 20260526_0049
Create Date: 2026-05-26 03:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260526_0050"
down_revision: Union[str, None] = "20260526_0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operator_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operator_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column(
            "details",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "logged_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_operator_actions_operator_logged",
        "operator_actions",
        ["operator_id", "logged_at"],
    )
    op.create_index(
        "ix_operator_actions_target",
        "operator_actions",
        ["target_type", "target_id", "logged_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operator_actions_target", table_name="operator_actions"
    )
    op.drop_index(
        "ix_operator_actions_operator_logged",
        table_name="operator_actions",
    )
    op.drop_table("operator_actions")
