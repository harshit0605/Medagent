"""Add service_heartbeats — production observability heartbeat table.

Each scheduler loop writes one row per successful pass:
  - ``component`` identifies the loop (``scheduler.dispatch``,
    ``scheduler.dose_materialize``, ``scheduler.missed_dose_sweep``,
    ``scheduler.recap_sweep``, ``scheduler.care_gap_sweep``, etc.).
  - ``last_run_at`` (UTC) timestamps the pass.
  - ``last_outcome`` = "ok" / "error" / "skipped".
  - ``details`` is a tiny JSON blob with per-pass counters (e.g.
    {"dispatched": 3, "failed": 0, "skipped": 1}) so the /ops/health
    page can show meaningful per-loop activity, not just a green dot.

Single-row-per-component upsert pattern (UPSERT on ``component``).
Reads on this table are tiny and cheap — the ops console queries it
on every dashboard render.

Revision ID: 20260503_0020
Revises: 20260503_0019
Create Date: 2026-05-03 22:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260503_0020"
down_revision: Union[str, None] = "20260503_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "service_heartbeats",
        sa.Column("component", sa.String(length=64), nullable=False),
        sa.Column(
            "last_run_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "last_outcome", sa.String(length=16), nullable=False, server_default="ok"
        ),
        sa.Column(
            "details", sa.JSON(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "consecutive_errors",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("component"),
    )


def downgrade() -> None:
    op.drop_table("service_heartbeats")
