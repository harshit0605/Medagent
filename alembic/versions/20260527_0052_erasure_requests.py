"""Two-person (dual-control) approval for patient erasure.

Irreversible PHI erasure is the single most destructive operator action. A
single compromised / mistaken operator shouldn't be able to wipe a patient.
This table backs an optional 4-eyes flow (gated by ``ERASURE_DUAL_CONTROL``):
operator A files an ``erasure_request``; a DIFFERENT operator B approves it,
which then executes the scrub. Default-off so existing single-step erasure
keeps working until a deployment opts in.

States: ``pending`` → ``approved`` (executed) | ``rejected`` | ``cancelled``.

Revision ID: 20260527_0052
Revises: 20260527_0051
Create Date: 2026-05-27 01:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260527_0052"
down_revision: Union[str, None] = "20260527_0051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "erasure_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="pending"),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # At most one pending request per patient — a partial unique index keeps
    # ops from filing duplicate requests while one is open.
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_erasure_requests_patient_pending "
            "ON erasure_requests (patient_id) WHERE status = 'pending'"
        )
    )
    op.create_index(
        "ix_erasure_requests_status",
        "erasure_requests",
        ["status", "requested_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_erasure_requests_status", table_name="erasure_requests"
    )
    op.execute(
        sa.text("DROP INDEX IF EXISTS uq_erasure_requests_patient_pending")
    )
    op.drop_table("erasure_requests")
