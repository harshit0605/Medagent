"""Sliding-scale / conditional dosing rule on regimens (insulin support).

SoT §3A calls out insulin timing + glucose-conditional dosing. ``Regimen.dose``
is a single fixed string; insulin sliding-scale needs a glucose → units mapping.
This adds a nullable ``dosing_rule`` JSONB: when present, the regimen is a
sliding-scale insulin order. Shape (validated in services.orchestrator.insulin)::

    {
      "kind": "sliding_scale",
      "unit": "units",
      "bands": [{"min": 0, "max": 149, "units": 0}, ...],
      "low_glucose_threshold": 70,    # below → don't dose, escalate (hypo)
      "high_glucose_escalate": 400    # at/above → escalate (severe hyper)
    }

NULL ``dosing_rule`` = the existing fixed-dose behaviour, unchanged.

Revision ID: 20260527_0053
Revises: 20260527_0052
Create Date: 2026-05-27 02:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260527_0053"
down_revision: Union[str, None] = "20260527_0052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "regimens",
        sa.Column("dosing_rule", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("regimens", "dosing_rule")
