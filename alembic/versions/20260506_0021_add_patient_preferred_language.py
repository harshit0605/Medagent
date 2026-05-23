"""Add preferred_language to patients.

A patient-level locale hint that drives LLM-generated outbound copy
(post-visit recaps, doctor-reply suggestions, free-form bot replies).
Stored as an ISO-639-1 code with optional region tag — e.g. ``en``,
``hi``, ``ta``, ``en-IN``. The orchestrator validates against an
allowlist at write time so we don't end up with garbage codes the
LLM can't reasonably honour.

Default ``en`` keeps existing behaviour for every patient created
before this migration. Onboarding will eventually capture this on
first contact; until then ops sets it from the patient detail page.

Revision ID: 20260506_0021
Revises: 20260503_0020
Create Date: 2026-05-06 14:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260506_0021"
down_revision: Union[str, None] = "20260503_0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "patients",
        sa.Column(
            "preferred_language",
            sa.String(length=8),
            nullable=False,
            server_default="en",
        ),
    )


def downgrade() -> None:
    op.drop_column("patients", "preferred_language")
