"""Performance indexes for materializer + weekly-trend queries.

Two hot paths lacked supporting indexes:

  1. Scheduler materializers query pending events by ``event_type`` +
     ``status`` (then filter the payload). The existing
     ``(status, scheduled_for)`` index serves the dispatch queue but not the
     per-type materializer lookup. Add ``(event_type, status)``.

  2. The weekly-trend sweep scans ``metric_observations`` by
     ``source='patient_self_report' AND observed_at >= cutoff``. The existing
     indexes lead with ``patient_id`` / ``goal_id`` — neither helps this
     cross-patient scan. Add ``(source, observed_at)``.

Revision ID: 20260510_0043
Revises: 20260510_0042
Create Date: 2026-05-10 15:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260510_0043"
down_revision: Union[str, None] = "20260510_0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_scheduled_events_type_status",
        "scheduled_events",
        ["event_type", "status"],
    )
    op.create_index(
        "ix_metric_observations_source_observed",
        "metric_observations",
        ["source", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_metric_observations_source_observed",
        table_name="metric_observations",
    )
    op.drop_index(
        "ix_scheduled_events_type_status",
        table_name="scheduled_events",
    )