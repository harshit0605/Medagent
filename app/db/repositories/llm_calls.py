"""Repo helpers for the ``llm_call_logs`` table.

The table is append-only — every OpenAI call writes one row at
ingest time. Reads are exclusively analytics aggregations
(``summarize`` for the cost dashboard).

A single ``record`` helper rather than a context manager because
the orchestration logic (start time, completion-vs-error
branches) lives in ``services.orchestrator.llm_tracking`` —
this module is the persistence boundary, not the call boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LlmCallLog


async def record(
    session: AsyncSession,
    *,
    call_kind: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    latency_ms: int | None = None,
    cost_usd_micros: int = 0,
    patient_id: str | None = None,
    message_id: str | None = None,
    error: str | None = None,
    occurred_at: datetime | None = None,
) -> LlmCallLog:
    """Persist one LLM call record. Caller is the
    ``track_llm_call`` context manager — no other module should
    write here directly so the schema stays consistent.

    Caller commits — the tracker batches the write into the
    surrounding request's transaction so a partial failure
    rolls back cleanly.
    """
    row = LlmCallLog(
        occurred_at=occurred_at or datetime.now(timezone.utc),
        patient_id=patient_id,
        message_id=message_id,
        call_kind=call_kind,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
        cost_usd_micros=cost_usd_micros,
        # ``error`` column is String(255) — truncate longer
        # exception messages so a verbose stack trace doesn't
        # blow the column.
        error=(error[:255] if error else None),
    )
    session.add(row)
    await session.flush()
    return row


async def summarize(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate cost + latency over a window for the analytics
    dashboard. One round-trip to the DB returning:

        {
          "since": ISO,
          "until": ISO,
          "total_calls": int,
          "total_tokens": int,
          "total_cost_usd_micros": int,
          "by_call_kind": [
              {"call_kind": str, "calls": int, "tokens": int, "cost_usd_micros": int}
          ],
          "by_model": [
              {"model": str, "calls": int, "tokens": int, "cost_usd_micros": int}
          ],
          "errors_count": int,
        }

    The latency p50/p95 and per-patient top-N live in dedicated
    helpers (separate queries) — this one keeps the most-used
    aggregations cheap and bounded.
    """
    when = until or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    # Aggregate by call_kind.
    kind_stmt = (
        select(
            LlmCallLog.call_kind,
            func.count().label("calls"),
            func.coalesce(func.sum(LlmCallLog.total_tokens), 0).label("tokens"),
            func.coalesce(
                func.sum(LlmCallLog.cost_usd_micros), 0
            ).label("cost_usd_micros"),
        )
        .where(LlmCallLog.occurred_at >= since)
        .where(LlmCallLog.occurred_at <= when)
        .group_by(LlmCallLog.call_kind)
        .order_by(
            desc(func.sum(LlmCallLog.cost_usd_micros).label("c"))
        )
    )
    kind_rows = (await session.execute(kind_stmt)).all()
    by_call_kind = [
        {
            "call_kind": k,
            "calls": int(c),
            "tokens": int(t),
            "cost_usd_micros": int(cost),
        }
        for k, c, t, cost in kind_rows
    ]

    # Aggregate by model.
    model_stmt = (
        select(
            LlmCallLog.model,
            func.count().label("calls"),
            func.coalesce(func.sum(LlmCallLog.total_tokens), 0).label("tokens"),
            func.coalesce(
                func.sum(LlmCallLog.cost_usd_micros), 0
            ).label("cost_usd_micros"),
        )
        .where(LlmCallLog.occurred_at >= since)
        .where(LlmCallLog.occurred_at <= when)
        .group_by(LlmCallLog.model)
        .order_by(
            desc(func.sum(LlmCallLog.cost_usd_micros).label("c"))
        )
    )
    model_rows = (await session.execute(model_stmt)).all()
    by_model = [
        {
            "model": m,
            "calls": int(c),
            "tokens": int(t),
            "cost_usd_micros": int(cost),
        }
        for m, c, t, cost in model_rows
    ]

    # Top-line totals — single query.
    totals_stmt = select(
        func.count().label("calls"),
        func.coalesce(func.sum(LlmCallLog.total_tokens), 0).label("tokens"),
        func.coalesce(
            func.sum(LlmCallLog.cost_usd_micros), 0
        ).label("cost_usd_micros"),
    ).where(LlmCallLog.occurred_at >= since).where(
        LlmCallLog.occurred_at <= when
    )
    totals_row = (await session.execute(totals_stmt)).one()

    # Error count — separate scalar query. Cleaner than a CASE
    # cast inside the totals aggregate; Postgres optimises the
    # partial-index lookup on (occurred_at, error IS NOT NULL).
    errors_stmt = (
        select(func.count())
        .select_from(LlmCallLog)
        .where(LlmCallLog.occurred_at >= since)
        .where(LlmCallLog.occurred_at <= when)
        .where(LlmCallLog.error.is_not(None))
    )
    errors_count = int((await session.execute(errors_stmt)).scalar() or 0)

    return {
        "since": since.isoformat(),
        "until": when.isoformat(),
        "total_calls": int(totals_row.calls),
        "total_tokens": int(totals_row.tokens),
        "total_cost_usd_micros": int(totals_row.cost_usd_micros),
        "errors_count": errors_count,
        "by_call_kind": by_call_kind,
        "by_model": by_model,
    }


async def latency_percentiles(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime | None = None,
) -> dict[str, Any]:
    """Compute p50 + p95 + p99 latency over a window. Postgres'
    ``percentile_cont`` is the right primitive — single query,
    server-side. Skipped on rows where ``latency_ms IS NULL``
    (errored calls) so the percentiles reflect actual response
    times, not exception paths.
    """
    when = until or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    stmt = select(
        func.percentile_cont(0.5)
        .within_group(LlmCallLog.latency_ms.asc())
        .label("p50"),
        func.percentile_cont(0.95)
        .within_group(LlmCallLog.latency_ms.asc())
        .label("p95"),
        func.percentile_cont(0.99)
        .within_group(LlmCallLog.latency_ms.asc())
        .label("p99"),
        func.coalesce(
            func.avg(LlmCallLog.latency_ms), 0
        ).label("mean"),
    ).where(
        LlmCallLog.occurred_at >= since
    ).where(
        LlmCallLog.occurred_at <= when
    ).where(
        LlmCallLog.latency_ms.is_not(None)
    )
    row = (await session.execute(stmt)).one()
    return {
        "p50_ms": int(row.p50) if row.p50 is not None else None,
        "p95_ms": int(row.p95) if row.p95 is not None else None,
        "p99_ms": int(row.p99) if row.p99 is not None else None,
        "mean_ms": int(row.mean) if row.mean else None,
    }


async def top_patients_by_cost(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Top-N patients by total LLM cost in the window. Identifies
    expensive patients — usually long booking conversations or
    heavy classification workloads. NULL patient_ids are
    excluded (those are platform-level calls)."""
    when = until or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    stmt = (
        select(
            LlmCallLog.patient_id,
            func.count().label("calls"),
            func.coalesce(
                func.sum(LlmCallLog.total_tokens), 0
            ).label("tokens"),
            func.coalesce(
                func.sum(LlmCallLog.cost_usd_micros), 0
            ).label("cost_usd_micros"),
        )
        .where(LlmCallLog.occurred_at >= since)
        .where(LlmCallLog.occurred_at <= when)
        .where(LlmCallLog.patient_id.is_not(None))
        .group_by(LlmCallLog.patient_id)
        .order_by(
            desc(func.sum(LlmCallLog.cost_usd_micros).label("c"))
        )
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "patient_id": pid,
            "calls": int(c),
            "tokens": int(t),
            "cost_usd_micros": int(cost),
        }
        for pid, c, t, cost in rows
    ]
