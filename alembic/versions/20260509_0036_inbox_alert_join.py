"""Denormalise clinical alert severity onto inbound_classifications.

The ops console's /inbox view needs to render a small severity
badge when the underlying message tripped the slice-10 triage
classifier into raising an alert. The cleanest signal is on
the inbox row itself.

Adding a denormalised ``clinical_severity`` column instead of
joining via ``message_log_id`` because:

    1. ``inbound_classifications.message_id`` is the Meta wamid
       (string), but ``clinical_alerts.message_id`` is the FK to
       ``message_log.id`` (int). They're different IDs.
    2. The wamid lives inside ``message_log.message`` JSON for
       inbound rows, not in a queryable column. Joining would
       need a Postgres JSON-path query per row.
    3. The set of values is tiny — null, "high", "critical" —
       so the storage cost is negligible.
    4. We already do similar denormalisation on this table
       (``urgency`` is the inbox classifier's view, this would
       be the triage classifier's view, side-by-side).

Filled by ``_persist_clinical_alert_if_urgent`` in
``services.orchestrator.main`` after a clinical_alerts row is
created. Existing rows keep ``NULL`` — interpreted as "no
clinical alert tied to this inbox row" which is the correct
default for back-history (slice 10 hadn't shipped, so no
alerts existed).

Revision ID: 20260509_0036
Revises: 20260509_0035
Create Date: 2026-05-09 13:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260509_0036"
down_revision: Union[str, None] = "20260509_0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inbound_classifications",
        sa.Column(
            "clinical_severity",
            sa.String(length=16),
            nullable=True,
            comment=(
                "Triage classifier severity ('high' or 'critical') "
                "when this message tripped a clinical_alerts row. "
                "NULL when the triage classifier returned a lower "
                "severity or didn't run."
            ),
        ),
    )
    # Partial index — most rows are NULL (low-severity / non-text).
    # Indexing only the non-null subset keeps the index small while
    # still serving the inbox UI's "show me alerts" filter cheaply.
    op.create_index(
        "ix_inbound_classifications_clinical_severity",
        "inbound_classifications",
        ["clinical_severity", "created_at"],
        postgresql_where=sa.text("clinical_severity IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inbound_classifications_clinical_severity",
        table_name="inbound_classifications",
    )
    op.drop_column(
        "inbound_classifications", "clinical_severity"
    )
