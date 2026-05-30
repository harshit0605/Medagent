"""Partial index for the per-patient delivery reconciliation sweep.

The delivery_reconcile_sweep asks, hourly: which recipients have N+ failed
deliveries (and zero successes) in the last few days? That's a grouped scan
filtered to ``status = 'failed'`` and time-bounded on ``updated_at``. A
partial index on ``(recipient_id, updated_at) WHERE status = 'failed'`` keeps
that scan tight — it only indexes the (small) failed subset, and serves both
the per-recipient grouping and the time-window bound.

Scope note: the original C1 plan also called for ``message_log(patient_id,
occurred_at DESC)`` and ``audit_records(actor, created_at DESC)``. Both were
dropped after checking the live schema:
  * message_log already has ``ix_message_log_patient_id_occurred_at`` (ASC);
    Postgres serves the newest-first inbox via a backward index scan, so a
    DESC duplicate would be pure write overhead.
  * audit_records has no ``actor`` column (actor lives in details JSONB);
    the operator-filter use case is now served by the dedicated
    ``operator_actions`` table and its ``(operator_id, logged_at)`` index.

Revision ID: 20260527_0051
Revises: 20260526_0050
Create Date: 2026-05-27 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "20260527_0051"
down_revision: Union[str, None] = "20260526_0050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        text(
            "CREATE INDEX ix_whatsapp_status_failed_recipient "
            "ON whatsapp_message_statuses (recipient_id, updated_at) "
            "WHERE status = 'failed'"
        )
    )


def downgrade() -> None:
    op.execute(
        text("DROP INDEX IF EXISTS ix_whatsapp_status_failed_recipient")
    )
