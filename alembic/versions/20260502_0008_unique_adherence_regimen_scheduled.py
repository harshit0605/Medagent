"""unique constraint on adherence_events (regimen_id, scheduled_at)

Materializer idempotency: the dose-reminder background loop runs every N
minutes for a 48h window, so it sees the same upcoming occurrences over
and over. The unique constraint stops a duplicate AdherenceEvent ever
landing if a race or buggy caller skips the Python-side dedupe.

Regimen_id can be NULL (legacy AdherenceEvents may not have a regimen),
but Postgres allows multiple NULLs in a unique index by default — that's
fine for our purposes.

Revision ID: 20260502_0008
Revises: 20260430_0007
Create Date: 2026-05-02 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260502_0008"
down_revision: Union[str, None] = "20260430_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_adherence_regimen_scheduled",
        "adherence_events",
        ["regimen_id", "scheduled_at"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_adherence_regimen_scheduled",
        "adherence_events",
        type_="unique",
    )
