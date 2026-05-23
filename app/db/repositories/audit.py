from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditRecord


async def log_policy_decision(
    session: AsyncSession,
    *,
    patient_id: str,
    outbound_mode: str | None,
    flow_action: str | None,
    reason_codes: list[str],
    details: dict[str, Any] | None = None,
    logged_at: datetime | None = None,
) -> AuditRecord:
    row = AuditRecord(
        record_type="policy_decision",
        patient_id=patient_id,
        outbound_mode=outbound_mode,
        flow_action=flow_action,
        reason_codes=list(reason_codes),
        details=details or {},
        logged_at=logged_at or datetime.now(timezone.utc),
    )
    session.add(row)
    await session.flush()
    return row


async def log_gateway_outbound(
    session: AsyncSession,
    *,
    patient_id: str,
    mode: str,
    reason_codes: list[str],
    logged_at: datetime | None = None,
) -> AuditRecord:
    row = AuditRecord(
        record_type="whatsapp_outbound_policy",
        patient_id=patient_id,
        outbound_mode=mode,
        flow_action=None,
        reason_codes=list(reason_codes),
        details={},
        logged_at=logged_at or datetime.now(timezone.utc),
    )
    session.add(row)
    await session.flush()
    return row


async def log_workflow_summary(
    session: AsyncSession,
    *,
    patient_id: str,
    outbound_mode: str | None,
    flow_action: str | None,
    reason_codes: list[str],
    details: dict[str, Any] | None = None,
    logged_at: datetime | None = None,
) -> AuditRecord:
    row = AuditRecord(
        record_type="workflow_summary",
        patient_id=patient_id,
        outbound_mode=outbound_mode,
        flow_action=flow_action,
        reason_codes=list(reason_codes),
        details=details or {},
        logged_at=logged_at or datetime.now(timezone.utc),
    )
    session.add(row)
    await session.flush()
    return row


async def search(
    session: AsyncSession,
    *,
    patient_id: str | None = None,
    record_type: str | None = None,
    reason_code: str | None = None,
    flow_action: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AuditRecord], int]:
    """Filtered + paginated audit-records search.

    Returns ``(rows, total_count)``. Rows are newest-first
    (``logged_at DESC``), capped at ``limit``. ``total_count`` is
    the unpaginated count so the UI can render "showing 1-50 of
    312" without a second query round-trip.

    All filters are AND-combined. Missing filters mean "any".
    ``reason_code`` matches if the JSON ``reason_codes`` array
    contains the given string — Postgres' JSON containment lets
    us avoid a full table scan.

    Used by the ops-console /audit-search page so a debugging
    session doesn't need a database client. The covering index
    ``ix_audit_records_patient_logged`` makes patient-scoped
    queries fast even at scale; full-table queries (no patient
    filter) need the date range + limit to bound runtime.
    """
    conditions = []
    if patient_id:
        conditions.append(AuditRecord.patient_id == patient_id)
    if record_type:
        conditions.append(AuditRecord.record_type == record_type)
    if flow_action:
        conditions.append(AuditRecord.flow_action == flow_action)
    if since is not None:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        conditions.append(AuditRecord.logged_at >= since)
    if until is not None:
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        conditions.append(AuditRecord.logged_at <= until)
    if reason_code:
        # Postgres JSONB containment: ``reason_codes::jsonb @>
        # '["x"]'::jsonb`` selects rows whose JSON array includes
        # the value. The column itself is a plain ``JSON`` type
        # (not ``JSONB``) so SQLAlchemy's ``.contains`` would
        # lower to a string LIKE — wrong operator. Cast the
        # column to ``JSONB`` inline so the containment operator
        # gets used.
        conditions.append(
            AuditRecord.reason_codes.cast(JSONB).contains(
                [reason_code]
            )
        )

    rows_stmt = select(AuditRecord)
    count_stmt = select(func.count()).select_from(AuditRecord)
    for cond in conditions:
        rows_stmt = rows_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    rows_stmt = (
        rows_stmt.order_by(desc(AuditRecord.logged_at))
        .limit(limit)
        .offset(offset)
    )

    rows = list((await session.execute(rows_stmt)).scalars().all())
    total = int((await session.execute(count_stmt)).scalar() or 0)
    return rows, total


async def log_patient_data_export(
    session: AsyncSession,
    *,
    patient_id: str,
    actor: str,
    reason_codes: list[str],
    details: dict[str, Any] | None = None,
    logged_at: datetime | None = None,
) -> AuditRecord:
    """DSAR right-of-access audit row. ``record_type`` lives in the
    64-char column — plenty of room for ``"patient_data_export"`` —
    so the regulator-trace tooling can grep on the type without
    squeezing into the 16-char ``flow_action`` field used by the
    policy-decision logger."""
    body = dict(details or {})
    body["actor"] = actor
    row = AuditRecord(
        record_type="patient_data_export",
        patient_id=patient_id,
        outbound_mode=None,
        flow_action=None,
        reason_codes=list(reason_codes),
        details=body,
        logged_at=logged_at or datetime.now(timezone.utc),
    )
    session.add(row)
    await session.flush()
    return row
