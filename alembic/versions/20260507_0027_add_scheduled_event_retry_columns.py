"""Add retry-policy columns to scheduled_events.

The existing failure model is binary: ``mark_failed`` writes the
error and the row stays at ``status=failed`` forever. A transient
network blip during a reminder's dispatch window means the patient
silently never gets the reminder. For a medical bot this is bad —
we want the system to retry transient failures and only give up
after a reasonable number of attempts.

Three new columns:

    attempt_count INT NOT NULL DEFAULT 0
        Cumulative number of dispatch attempts made for this row.
        Incremented on every ``mark_failed``. The retry policy is
        a function of this value: backoff grows exponentially.

    next_retry_at TIMESTAMPTZ NULL
        When the row becomes eligible for another dispatch attempt.
        NULL on a row that's:
            - never been dispatched (``status=pending``)
            - successfully dispatched (``status=dispatched``)
            - skipped (``status=skipped``)
            - in the dead-letter queue: failed AND attempts
              exhausted. NULL here means "do not retry".

    last_failed_at TIMESTAMPTZ NULL
        When the most recent failure occurred. Useful for ops:
        "this DLQ item last attempted 3 days ago" is more
        diagnostic than just the error string.

The covering index ``ix_scheduled_events_status_next_retry`` makes
the dispatcher's "fetch due + fetch retries due" query a single
indexed scan rather than two queries.

Revision ID: 20260507_0027
Revises: 20260507_0026
Create Date: 2026-05-08 09:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260507_0027"
down_revision: Union[str, None] = "20260507_0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "scheduled_events",
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "scheduled_events",
        sa.Column(
            "next_retry_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "scheduled_events",
        sa.Column(
            "last_failed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Covering index for the dispatcher's two combined fetches
    # (pending due + failed-with-retry due). The partial WHERE
    # keeps the index small — only events that COULD be due go
    # in.
    op.create_index(
        "ix_scheduled_events_status_next_retry",
        "scheduled_events",
        ["status", "next_retry_at"],
        postgresql_where=sa.text(
            "status IN ('pending', 'failed')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scheduled_events_status_next_retry",
        table_name="scheduled_events",
    )
    op.drop_column("scheduled_events", "last_failed_at")
    op.drop_column("scheduled_events", "next_retry_at")
    op.drop_column("scheduled_events", "attempt_count")
