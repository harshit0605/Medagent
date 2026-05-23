"""Add llm_call_logs table + inbound_classifications.request_duration_ms.

The bot relies on OpenAI for compose, intent detection, inbox
classification, recap generation, language detection, prescription
vision, and the booking agent's ReAct loop. We currently have no
per-call cost or latency telemetry — running blind on "are we
operating at acceptable cost?" before scaling to more clinics is
risky.

``llm_call_logs`` stores one row per OpenAI API call, capturing:

    - context: ``patient_id``, ``message_id``, ``call_kind`` so
      analytics can break down by per-patient + per-handler
    - tokens: ``prompt_tokens`` / ``completion_tokens`` / ``total``
      from the OpenAI usage object
    - cost: ``cost_usd_micros`` (10⁻⁶ USD integer) computed from
      a per-model rate table at ingest time. Stored as integer
      micros so a sum over millions of rows doesn't drift due to
      floating-point.
    - latency: ``latency_ms`` for the OpenAI call alone (vs the
      end-to-end /route latency stamped on
      inbound_classifications)
    - error: ``error`` if the call raised; otherwise NULL

``inbound_classifications.request_duration_ms`` captures the
end-to-end latency of /route (inbound arrival → response built).
Different concern from llm_call_logs.latency_ms — the /route
duration includes DB writes, handler dispatch, audit log writes,
multiple LLM calls strung together, etc.

Three indexes serve the analytics queries:
    - (occurred_at) — top-line "last 24h cost" query
    - (call_kind, occurred_at) — "by handler" breakdown
    - (patient_id, occurred_at) WHERE patient_id IS NOT NULL —
      "top-N most expensive patients" query

Revision ID: 20260508_0031
Revises: 20260508_0030
Create Date: 2026-05-08 16:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260508_0031"
down_revision: Union[str, None] = "20260508_0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_call_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("patient_id", sa.String(length=128), nullable=True),
        sa.Column("message_id", sa.String(length=128), nullable=True),
        sa.Column("call_kind", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("cost_usd_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_llm_call_logs_occurred_at",
        "llm_call_logs",
        ["occurred_at"],
    )
    op.create_index(
        "ix_llm_call_logs_call_kind_occurred",
        "llm_call_logs",
        ["call_kind", "occurred_at"],
    )
    op.create_index(
        "ix_llm_call_logs_patient_occurred",
        "llm_call_logs",
        ["patient_id", "occurred_at"],
        postgresql_where=sa.text("patient_id IS NOT NULL"),
    )

    op.add_column(
        "inbound_classifications",
        sa.Column("request_duration_ms", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inbound_classifications", "request_duration_ms")
    op.drop_index(
        "ix_llm_call_logs_patient_occurred", table_name="llm_call_logs"
    )
    op.drop_index(
        "ix_llm_call_logs_call_kind_occurred", table_name="llm_call_logs"
    )
    op.drop_index("ix_llm_call_logs_occurred_at", table_name="llm_call_logs")
    op.drop_table("llm_call_logs")
