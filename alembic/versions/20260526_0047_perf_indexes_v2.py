"""Hot-path indexes flagged by the perf audit.

Two indexes added — both directly support queries we run on every inbound
or every dashboard load:

* ``ix_clinical_alerts_patient_status`` — composite on
  ``(patient_id, status)`` for the on-call repager + ops dashboard
  "open alerts per patient" queries. The existing
  ``(status, severity, created_at)`` is great for status-first
  scans across all patients, and the ``(patient_id)``-only index
  is selective per patient but not by status. The composite serves
  both ``WHERE patient_id = ? AND status = 'open'`` and
  patient-only lookups via its leading column.

* ``ix_metric_observations_goal_observed_desc`` — DESC variant of
  the existing ``(goal_id, observed_at)`` for the care-plan-goal
  history view, which paginates ``ORDER BY observed_at DESC
  LIMIT 50``. Postgres can scan an ASC btree backwards, but a
  DESC-built index makes ``LIMIT`` queries strictly cheaper
  (no reverse fetch step). Worth it for a view that loads on
  every patient detail render.

The third index in the audit punch list — ``adherence_events
(regimen_id, scheduled_at)`` — is already covered by the existing
``UniqueConstraint("regimen_id", "scheduled_at")``, which Postgres
implements as a unique B-tree index. No-op skipped here.

Revision ID: 20260526_0047
Revises: 20260524_0046
Create Date: 2026-05-26 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "20260526_0047"
down_revision: Union[str, None] = "20260524_0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_clinical_alerts_patient_status",
        "clinical_alerts",
        ["patient_id", "status"],
    )
    # Raw DDL — ``op.create_index`` doesn't accept per-column ASC/DESC
    # ordering. The DESC modifier matters here because the consuming query
    # paginates newest-first with a LIMIT; an ASC index handles it via a
    # backward scan but loses some efficiency.
    op.execute(
        text(
            "CREATE INDEX ix_metric_observations_goal_observed_desc "
            "ON metric_observations (goal_id, observed_at DESC)"
        )
    )


def downgrade() -> None:
    op.execute(
        text("DROP INDEX IF EXISTS ix_metric_observations_goal_observed_desc")
    )
    op.drop_index(
        "ix_clinical_alerts_patient_status",
        table_name="clinical_alerts",
    )
