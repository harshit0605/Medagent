"""Add on-call flag + clinical-alert paging audit columns.

Two changes that go together because they support a single
feature: when a critical clinical alert lands, page a doctor
on WhatsApp and audit who got paged when.

``doctors.is_on_call``:
    A simple boolean. We don't yet have a time-of-day rota
    (that's a future slice); on-call is the persistent
    "include me in the paging fanout" flag. Indexed so the
    fallback query (``find any on-call doctor``) is cheap.

``clinical_alerts.paged_*``:
    Audit trail for the paging path. ``paged_doctor_id`` is
    the doctor who received the most recent page (re-pages
    update this). ``paged_at`` is the most-recent attempt's
    timestamp. ``paged_attempts`` increments on every send so
    the re-page sweep can cap at 3 attempts and stop annoying
    a doctor who genuinely missed the page.

Doctor selection rule (lives in the pager service, not the
schema):

    1. Most recent doctor from this patient's confirmed
       appointments — they own clinical context.
    2. Fallback: any doctor with ``is_on_call=True``.
    3. None — alert remains in queue, audit shows
       ``paged_doctor_id IS NULL`` so ops can see "no doctor
       reachable" and act manually.

Revision ID: 20260509_0035
Revises: 20260509_0034
Create Date: 2026-05-09 11:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260509_0035"
down_revision: Union[str, None] = "20260509_0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "doctors",
        sa.Column(
            "is_on_call",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "ix_doctors_is_on_call",
        "doctors",
        ["is_on_call"],
        postgresql_where=sa.text("is_on_call = true"),
    )

    op.add_column(
        "clinical_alerts",
        sa.Column(
            "paged_doctor_id",
            sa.Integer(),
            sa.ForeignKey("doctors.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "clinical_alerts",
        sa.Column(
            "paged_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "clinical_alerts",
        sa.Column(
            "paged_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("clinical_alerts", "paged_attempts")
    op.drop_column("clinical_alerts", "paged_at")
    op.drop_column("clinical_alerts", "paged_doctor_id")
    op.drop_index("ix_doctors_is_on_call", table_name="doctors")
    op.drop_column("doctors", "is_on_call")
