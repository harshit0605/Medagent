"""Add orders table (partner / refill execute layer).

Activates the previously-dead ``Order`` schema. An order is created when a
patient reorders a medication (refill "Reorder" tap) or ops/a partner places
one on their behalf; a replaceable pharmacy adapter assigns the ``partner``
and an optional deep-link / partner_order_id. Status walks
pending → processing → shipped → delivered (or canceled).

Substitution columns support the ``substitution_approval_v1`` flow: a partner
proposes a generic/alternative, we ask the patient to approve, and record the
outcome on the order.

``status`` and ``substitution_status`` are plain Strings (not PG enums),
matching the care_plan_goals / pregnancies / asthma convention — clinical +
fulfillment states churn faster than enum migrations ship. The
:class:`OrderStatus` constants remain the canonical values.

Revision ID: 20260510_0040
Revises: 20260510_0039
Create Date: 2026-05-10 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260510_0040"
down_revision: Union[str, None] = "20260510_0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The orders table from migration 0001 is DEAD (defined, never written) —
    # old 4-column shape + an ``order_status`` PG enum. It's empty and a leaf
    # table, so we drop + recreate it with the execute-layer schema (String
    # status per the recent convention, regimen link, med/dose snapshots, and
    # substitution columns) rather than a fragile multi-step ALTER + enum cast.
    op.drop_table("orders")
    op.execute("DROP TYPE IF EXISTS order_status")
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "patient_id",
            sa.Integer(),
            sa.ForeignKey("patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "regimen_id",
            sa.Integer(),
            sa.ForeignKey("regimens.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("medication_name", sa.String(length=255), nullable=False),
        sa.Column("dose", sa.String(length=128), nullable=True),
        sa.Column("quantity", sa.String(length=64), nullable=True),
        sa.Column(
            "partner",
            sa.String(length=128),
            nullable=False,
            server_default="manual",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("partner_order_id", sa.String(length=128), nullable=True),
        sa.Column("partner_deeplink", sa.Text(), nullable=True),
        sa.Column("receipt_url", sa.Text(), nullable=True),
        sa.Column("substitution_status", sa.String(length=32), nullable=True),
        sa.Column(
            "substitution_medication", sa.String(length=255), nullable=True
        ),
        sa.Column("substitution_note", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("requested_via", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_orders_patient_status", "orders", ["patient_id", "status"]
    )
    op.create_index(
        "ix_orders_regimen_status", "orders", ["regimen_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_orders_regimen_status", table_name="orders")
    op.drop_index("ix_orders_patient_status", table_name="orders")
    op.drop_table("orders")
    # Restore the original (dead) orders table + order_status enum from 0001.
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("partner", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "processing",
                "shipped",
                "delivered",
                "canceled",
                name="order_status",
            ),
            nullable=False,
        ),
        sa.Column("partner_order_id", sa.String(length=128), nullable=True),
        sa.Column("receipt_url", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
